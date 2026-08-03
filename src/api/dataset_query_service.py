"""Governed query path for user-selected single- and multi-relation datasets."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator
from sqlglot import exp

from api.datasource_service import DataSourceService
from data_agent.datasources import (
    DataSourceRegistryError,
    SemanticBindingRecord,
    SemanticGraphBindingRecord,
    SemanticFieldMapping,
    SemanticRelationship,
)
from data_agent.relationships.compiler import bind_join_plan
from data_agent.relationships.grain import FanoutGuard
from data_agent.relationships.router import GraphRouteError, GraphRouteRequest, GraphRouteResolver
from data_agent.runtime.binding import PreparedQuery, QueryParameter
from data_agent.runtime.dependencies import ModelClient
from data_agent.runtime.errors import AgentError, ErrorCode
from data_agent.runtime.events import (
    AgentEvent,
    AgentEventType,
    RunCompletedPayload,
    RunFailedPayload,
    RunStartedPayload,
)
from data_agent.runtime.models import (
    AgentMode,
    AgentRequest,
    AgentResponse,
    AgentRow,
    AgentTraceEntry,
    ChartSpec,
    PrincipalContext,
)
from data_agent.skills.models import (
    AnalysisType,
    LogicalQueryPlan,
    RelationshipRouteEvidence,
    ResultShape,
)
from data_agent.tools import AccessGrant, CredentialLease
from data_agent.tools.connectors import ConnectorError, ConnectorErrorCode
from data_agent.tools.schemas import CatalogSnapshot, TabularResult


NonBlankText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
SafeAlias = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        pattern=r"^[a-z][a-z0-9_]{0,62}$",
    ),
]
Scalar = str | int | float | bool

_SYSTEM_PROMPT = (
    "You are a governed dataset query planner. Return exactly one JSON object "
    "matching the supplied JSON Schema. Never return SQL or physical table/column "
    "names. Use only logical refs from logicalCatalog. If the requested metric is "
    "not defined by the catalog, return needs_clarification or unsupported instead "
    "of silently replacing it with another metric. For follow-ups, return only a "
    "DatasetPlanPatch and preserve every prior plan field not explicitly changed. "
    "Prefer a small result and never exceed the schema limit."
)
_FENCED_JSON = re.compile(
    r"```(?:json)?\s*(\{.*\})\s*```",
    re.DOTALL | re.IGNORECASE,
)


class DatasetPlanModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class DatasetPlanStatus(StrEnum):
    READY = "ready"
    NEEDS_CLARIFICATION = "needs_clarification"
    UNSUPPORTED = "unsupported"


class DatasetFilterOperator(StrEnum):
    EQ = "eq"
    NEQ = "neq"
    GT = "gt"
    GTE = "gte"
    LT = "lt"
    LTE = "lte"
    IN = "in"
    CONTAINS = "contains"
    IS_NULL = "is_null"
    IS_NOT_NULL = "is_not_null"


class DatasetFilter(DatasetPlanModel):
    ref: NonBlankText
    operator: DatasetFilterOperator
    value: Scalar | tuple[Scalar, ...] | None = None

    @model_validator(mode="after")
    def validate_value(self) -> "DatasetFilter":
        if self.operator in {
            DatasetFilterOperator.IS_NULL,
            DatasetFilterOperator.IS_NOT_NULL,
        }:
            if self.value is not None:
                raise ValueError("null filters cannot include a value")
        elif self.operator == DatasetFilterOperator.IN:
            if not isinstance(self.value, tuple) or not self.value:
                raise ValueError("in filters require a non-empty value list")
        elif self.value is None or isinstance(self.value, tuple):
            raise ValueError("scalar filters require one scalar value")
        return self


class DatasetAggregation(DatasetPlanModel):
    ref: NonBlankText
    operation: Literal["count", "count_distinct", "sum", "avg", "min", "max"]
    alias: SafeAlias


class DatasetOrdering(DatasetPlanModel):
    ref: NonBlankText
    direction: Literal["asc", "desc"] = "asc"


class DatasetQueryPlan(DatasetPlanModel):
    status: DatasetPlanStatus = DatasetPlanStatus.READY
    clarification_question: str | None = None
    analysis_type: Literal["detail", "aggregate"] | None = None
    select: tuple[NonBlankText, ...] = ()
    aggregations: tuple[DatasetAggregation, ...] = ()
    group_by: tuple[NonBlankText, ...] = ()
    filters: tuple[DatasetFilter, ...] = ()
    order_by: tuple[DatasetOrdering, ...] = ()
    limit: int = Field(default=100, ge=1, le=1000)

    @model_validator(mode="after")
    def validate_shape(self) -> "DatasetQueryPlan":
        if self.status != DatasetPlanStatus.READY:
            if not (self.clarification_question or "").strip():
                raise ValueError(
                    "non-ready plans require a clarification question"
                )
            if (
                self.analysis_type is not None
                or self.select
                or self.aggregations
                or self.group_by
                or self.filters
                or self.order_by
            ):
                raise ValueError(
                    "non-ready plans cannot include executable query fields"
                )
            return self
        if self.clarification_question is not None:
            raise ValueError("ready plans cannot include a clarification question")
        if self.analysis_type == "detail":
            if not self.select or self.aggregations or self.group_by:
                raise ValueError("detail plans require select fields only")
        elif self.analysis_type == "aggregate":
            if not self.aggregations:
                raise ValueError("aggregate plans require aggregations")
        else:
            raise ValueError("ready plans require an analysis type")
        aliases = tuple(item.alias for item in self.aggregations)
        if len(aliases) != len(set(aliases)):
            raise ValueError("aggregation aliases must be unique")
        for values in (self.select, self.group_by):
            if len(values) != len(set(values)):
                raise ValueError("logical refs must be unique")
        return self


class DatasetPlanPatch(DatasetPlanModel):
    status: DatasetPlanStatus = DatasetPlanStatus.READY
    clarification_question: str | None = None
    analysis_type: Literal["detail", "aggregate"] | None = None
    select: tuple[NonBlankText, ...] | None = None
    aggregations: tuple[DatasetAggregation, ...] | None = None
    group_by: tuple[NonBlankText, ...] | None = None
    filters: tuple[DatasetFilter, ...] | None = None
    add_filters: tuple[DatasetFilter, ...] = ()
    order_by: tuple[DatasetOrdering, ...] | None = None
    limit: int | None = Field(default=None, ge=1, le=1000)

    @model_validator(mode="after")
    def validate_patch(self) -> "DatasetPlanPatch":
        if self.status == DatasetPlanStatus.READY:
            if self.clarification_question is not None:
                raise ValueError(
                    "ready patches cannot include a clarification question"
                )
            return self
        if not (self.clarification_question or "").strip():
            raise ValueError(
                "non-ready patches require a clarification question"
            )
        executable = (
            self.analysis_type,
            self.select,
            self.aggregations,
            self.group_by,
            self.filters,
            self.order_by,
            self.limit,
        )
        if any(item is not None for item in executable) or self.add_filters:
            raise ValueError(
                "non-ready patches cannot include executable query fields"
            )
        return self

    def apply(self, prior: DatasetQueryPlan) -> DatasetQueryPlan:
        if self.status != DatasetPlanStatus.READY:
            return DatasetQueryPlan(
                status=self.status,
                clarification_question=self.clarification_question,
            )
        if prior.status != DatasetPlanStatus.READY:
            raise ValueError("cannot patch a non-ready dataset query plan")
        filters = (
            self.filters
            if self.filters is not None
            else prior.filters + self.add_filters
        )
        return DatasetQueryPlan(
            analysis_type=self.analysis_type or prior.analysis_type,
            select=prior.select if self.select is None else self.select,
            aggregations=(
                prior.aggregations
                if self.aggregations is None
                else self.aggregations
            ),
            group_by=(
                prior.group_by if self.group_by is None else self.group_by
            ),
            filters=filters,
            order_by=(
                prior.order_by if self.order_by is None else self.order_by
            ),
            limit=prior.limit if self.limit is None else self.limit,
        )


class DatasetPlanUpdate(DatasetPlanModel):
    mode: Literal["patch", "replace"]
    patch: DatasetPlanPatch | None = None
    plan: DatasetQueryPlan | None = None

    @model_validator(mode="after")
    def validate_update(self) -> "DatasetPlanUpdate":
        if self.mode == "patch":
            if self.patch is None or self.plan is not None:
                raise ValueError("patch updates require only patch")
        elif self.plan is None or self.patch is not None:
            raise ValueError("replace updates require only plan")
        return self


@dataclass(frozen=True, slots=True)
class DatasetConversationContext:
    prior_question: str
    prior_plan: DatasetQueryPlan


@dataclass(frozen=True, slots=True)
class DatasetPlanningResult:
    plan: DatasetQueryPlan
    contextualized_question: str


class DatasetLogicalPlanner:
    def __init__(self, model_client: ModelClient, *, max_attempts: int = 2) -> None:
        self._model_client = model_client
        self._max_attempts = max_attempts

    async def build_plan(
        self,
        *,
        question: str,
        binding: SemanticBindingRecord | SemanticGraphBindingRecord,
        catalog: CatalogSnapshot,
        conversation_context: DatasetConversationContext | None = None,
    ) -> DatasetPlanningResult:
        type_by_physical = {
            (relation.relation, column.name): column.data_type
            for relation in catalog.relations
            for column in relation.columns
        }
        if isinstance(binding, SemanticBindingRecord):
            logical_catalog = [
                {"ref": mapping.logical_ref, "type": type_by_physical.get((mapping.physical_relation, mapping.physical_column), "unknown")}
                for mapping in binding.mappings
            ]
        else:
            relation_by_node = {node.node_id: node.relation_id for node in binding.graph.nodes}
            relation_by_id = {relation.relation_id: relation.relation for relation in catalog.relations}
            column_by_id = {column.column_id: (relation.relation, column.name) for relation in catalog.relations for column in relation.columns}
            logical_catalog = [
                {"ref": mapping.logical_ref, "type": type_by_physical.get(column_by_id.get(mapping.column_id, ("", "")), "unknown")}
                for mapping in binding.mappings
                if relation_by_node.get(mapping.node_id) in relation_by_id
            ]
        if conversation_context is None:
            request = {
                "task": "create_dataset_query_plan",
                "question": question,
                "logicalCatalog": logical_catalog,
                "datasetQueryPlanSchema": DatasetQueryPlan.model_json_schema(),
            }
            contextualized_question = question
        else:
            request = {
                "task": "update_dataset_query_plan",
                "priorQuestion": conversation_context.prior_question,
                "priorPlan": conversation_context.prior_plan.model_dump(
                    mode="json"
                ),
                "followUpQuestion": question,
                "logicalCatalog": logical_catalog,
                "datasetPlanUpdateSchema": DatasetPlanUpdate.model_json_schema(),
                "instructions": (
                    "Choose mode=patch only for an elliptical or narrowing "
                    "follow-up; omitted patch fields inherit from priorPlan and "
                    "add_filters narrows without replacing aggregation/grouping. "
                    "Choose mode=replace for an independent new question."
                ),
            }
            contextualized_question = question
        prompt = json.dumps(request, ensure_ascii=False, separators=(",", ":"))
        failure = ""
        for attempt in range(self._max_attempts):
            raw = await self._model_client.complete(
                prompt,
                system=_SYSTEM_PROMPT,
                max_output_tokens=2048,
            )
            try:
                document = self._json_object(raw)
                plan, inherited = self._parse_plan(
                    document,
                    conversation_context=conversation_context,
                )
                if conversation_context is not None and inherited:
                    contextualized_question = (
                        f"{conversation_context.prior_question}；追问：{question}"
                    )
                plan = self._enforce_answerability(
                    question=question,
                    plan=plan,
                    logical_refs=tuple(
                        item["ref"] for item in logical_catalog
                    ),
                )
                return DatasetPlanningResult(
                    plan=plan,
                    contextualized_question=contextualized_question,
                )
            except ValueError as exc:
                failure = str(exc)
                if attempt + 1 >= self._max_attempts:
                    break
                prompt = json.dumps(
                    {
                        "task": "repair_dataset_query_plan",
                        "input": request,
                        "previousResponse": raw[:4000],
                        "validationErrors": failure[:4000],
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
        raise ValueError(
            "model failed to produce a valid DatasetQueryPlan: " + failure
        )

    @staticmethod
    def _parse_plan(
        document: dict[str, object],
        *,
        conversation_context: DatasetConversationContext | None,
    ) -> tuple[DatasetQueryPlan, bool]:
        if conversation_context is None:
            return DatasetQueryPlan.model_validate(document), False
        if document.get("mode") in {"patch", "replace"}:
            update = DatasetPlanUpdate.model_validate(document)
            if update.mode == "replace":
                assert update.plan is not None
                return update.plan, False
            assert update.patch is not None
            return update.patch.apply(conversation_context.prior_plan), True
        if "analysis_type" in document and any(
            key in document
            for key in ("select", "aggregations", "group_by")
        ):
            return DatasetQueryPlan.model_validate(document), False
        return (
            DatasetPlanPatch.model_validate(document).apply(
                conversation_context.prior_plan
            ),
            True,
        )

    @staticmethod
    def _enforce_answerability(
        *,
        question: str,
        plan: DatasetQueryPlan,
        logical_refs: tuple[str, ...],
    ) -> DatasetQueryPlan:
        if plan.status != DatasetPlanStatus.READY:
            return plan
        lowered_refs = tuple(item.casefold() for item in logical_refs)
        refund_intent = re.search(
            r"(退款|退货|refund|returned|return rate)",
            question,
            flags=re.IGNORECASE,
        )
        refund_semantics = any(
            re.search(r"(退款|退货|refund|return)", item)
            for item in lowered_refs
        )
        if refund_intent is not None and not refund_semantics:
            return DatasetQueryPlan(
                status=DatasetPlanStatus.NEEDS_CLARIFICATION,
                clarification_question=(
                    "当前数据集没有可识别的退款或退货字段，无法可靠计算退款率。"
                    "请提供退款判定字段或退款事件数据。"
                ),
            )
        rate_intent = re.search(
            r"(率|占比|比例|rate|ratio|percentage|percent)",
            question,
            flags=re.IGNORECASE,
        )
        rate_semantics = any(
            re.search(
                r"(率|占比|比例|rate|ratio|percentage|percent)",
                item,
            )
            for item in lowered_refs
        )
        if rate_intent is not None and not rate_semantics:
            return DatasetQueryPlan(
                status=DatasetPlanStatus.NEEDS_CLARIFICATION,
                clarification_question=(
                    "该问题需要派生比率，但当前语义绑定没有已定义的比率指标。"
                    "请明确分子、分母及统计口径后再查询。"
                ),
            )
        return plan

    @staticmethod
    def _json_object(value: str) -> dict[str, object]:
        text = value.strip()
        match = _FENCED_JSON.fullmatch(text)
        if match is not None:
            text = match.group(1)
        else:
            start, end = text.find("{"), text.rfind("}")
            if start < 0 or end < start:
                raise ValueError("model did not return a JSON object")
            text = text[start : end + 1]
        document = json.loads(text)
        if not isinstance(document, dict):
            raise ValueError("dataset query plan must be a JSON object")
        return document


class DatasetQueryCompiler:
    def compile(
        self,
        *,
        plan: DatasetQueryPlan,
        binding: SemanticBindingRecord | SemanticGraphBindingRecord,
        dialect: Literal["postgres", "sqlite", "duckdb"],
        schema_fingerprint: str,
        bundle_digest: str,
        catalog: CatalogSnapshot | None = None,
        _relation_table_overrides: dict[str, str] | None = None,
        _join_conditions_overrides: dict[str, tuple[tuple[str, str], ...]] | None = None,
        _alias_overrides: dict[str, str] | None = None,
    ) -> PreparedQuery:
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
        primary_relation = (
            binding.primary_relation
            or binding.mappings[0].physical_relation
        )
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

        def parameter(value: Scalar, purpose: Literal["filter", "limit"]):
            position = len(parameters) + 1
            parameters.append(
                QueryParameter(
                    position=position,
                    value=value,
                    purpose=purpose,
                )
            )
            return exp.Parameter(this=exp.Var(this=str(position)))

        selections: list[exp.Expression] = []
        output_aliases: dict[str, str] = {}
        selected_refs = plan.select if plan.analysis_type == "detail" else plan.group_by
        used_aliases: set[str] = set()
        for ref in selected_refs:
            alias = self._column_alias(ref, used_aliases)
            output_aliases[ref] = alias
            selections.append(
                exp.alias_(column(ref), alias, quoted=True)
            )
        for aggregation in plan.aggregations:
            argument = column(aggregation.ref)
            if aggregation.operation == "count_distinct":
                expression = exp.Count(
                    this=exp.Distinct(expressions=[argument])
                )
            else:
                expression = {
                    "count": exp.Count,
                    "sum": exp.Sum,
                    "avg": exp.Avg,
                    "min": exp.Min,
                    "max": exp.Max,
                }[aggregation.operation](this=argument)
            selections.append(
                exp.alias_(expression, aggregation.alias, quoted=True)
            )
            output_aliases[aggregation.alias] = aggregation.alias
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
                        table=exp.to_identifier(aliases[relationship.left_relation], quoted=True),
                    ),
                    expression=exp.Column(
                        this=exp.to_identifier(right_column, quoted=True),
                        table=exp.to_identifier(aliases[relationship.right_relation], quoted=True),
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
                predicates.append(
                    exp.Not(this=exp.Is(this=left, expression=exp.Null()))
                )
            elif item.operator == DatasetFilterOperator.IN:
                assert isinstance(item.value, tuple)
                predicates.append(
                    exp.In(
                        this=left,
                        expressions=[
                            parameter(value, "filter") for value in item.value
                        ],
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
            query.set(
                "group",
                exp.Group(expressions=[column(ref) for ref in plan.group_by]),
            )
        if plan.order_by:
            ordered: list[exp.Ordered] = []
            for item in plan.order_by:
                if item.ref in aggregation_aliases:
                    expression = exp.Column(
                        this=exp.to_identifier(item.ref, quoted=True)
                    )
                else:
                    expression = column(item.ref)
                ordered.append(
                    exp.Ordered(
                        this=expression,
                        desc=item.direction == "desc",
                    )
                )
            query.set("order", exp.Order(expressions=ordered))
        query.set(
            "limit",
            exp.Limit(expression=parameter(plan.limit, "limit")),
        )
        sql = query.sql(dialect=dialect, pretty=False)
        sql_hash = hashlib.sha256(sql.encode()).hexdigest()
        logical_plan = LogicalQueryPlan(
            analysis_type=(
                AnalysisType.DETAIL
                if plan.analysis_type == "detail"
                else AnalysisType.METRIC
            ),
            assumptions=(
                "compiled from an activated user-dataset semantic binding",
            ),
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
            raise ValueError("dataset query references unknown logical fields: " + ", ".join(sorted(unknown)))
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
        route_node_ids = tuple(dict.fromkeys((route.root_node_id, *(step.introduced_node_id for step in route.steps))))
        synthetic_mappings = [
            SemanticFieldMapping(
                logical_ref=mapping.logical_ref,
                physical_relation=mapping.node_id,
                physical_column=columns[mapping.column_id],
            )
            for mapping in binding.mappings
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
            _relation_table_overrides={node_id: relations[node_by_id[node_id].relation_id].relation for node_id in route_node_ids},
            _alias_overrides=bound.aliases,
            _join_conditions_overrides={
                edge_id: tuple((condition.left_column, condition.right_column) for condition in step.conditions)
                for edge_id, step in by_step.items()
            },
        )
        logical_plan = prepared.logical_plan.model_copy(
            update={
                "assumptions": (
                    *prepared.logical_plan.assumptions,
                    f"relationship route digest: {route.route_digest}",
                    "relationship nodes: " + ", ".join(route.included_node_ids),
                    "relationship edges: " + ", ".join(step.edge_id for step in route.steps),
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


class DataSourceQueryService:
    """Resolve pinned authority, plan logically, compile, and execute read-only."""

    def __init__(
        self,
        data_sources: DataSourceService,
        model_client: ModelClient,
    ) -> None:
        self._data_sources = data_sources
        self._planner = DatasetLogicalPlanner(model_client)
        self._compiler = DatasetQueryCompiler()

    async def run(
        self,
        request: AgentRequest,
        principal: PrincipalContext,
        *,
        conversation_context: DatasetConversationContext | None = None,
    ) -> AgentResponse:
        if (
            request.source_id is None
            or request.source_version is None
            or request.binding_id is None
            or request.binding_version is None
        ):
            raise ValueError("dataset query requires complete datasource pins")
        try:
            context = await self._data_sources.resolve_active_binding(
                tenant_id=principal.tenant_id,
                source_id=request.source_id,
                source_version=request.source_version,
                binding_id=request.binding_id,
                binding_version=request.binding_version,
                domain_id=request.domain_id,
            )
        except DataSourceRegistryError as exc:
            return self._failure(
                request,
                principal,
                ErrorCode.BINDING_STALE,
                str(exc),
                "resolve_datasource",
            )
        source = context.source
        snapshot = context.snapshot
        binding = context.binding
        connector = context.connector
        if request.conversation_id is not None:
            try:
                await self._data_sources.pin_conversation(
                    tenant_id=principal.tenant_id,
                    user_id=principal.user_id,
                    conversation_id=request.conversation_id,
                    binding=binding,
                )
            except DataSourceRegistryError as exc:
                return self._failure(
                    request,
                    principal,
                    ErrorCode.BINDING_STALE,
                    str(exc),
                    "pin_conversation_datasource",
                )
        try:
            planning = await self._planner.build_plan(
                question=request.question,
                binding=binding,
                catalog=snapshot.catalog,
                conversation_context=conversation_context,
            )
        except ValueError as exc:
            return self._failure(
                request,
                principal,
                ErrorCode.LOGICAL_PLAN_INVALID,
                str(exc),
                "plan_dataset_query",
            )
        plan = planning.plan
        contextualized_question = planning.contextualized_question
        serialized_plan = plan.model_dump(mode="json")
        if plan.status != DatasetPlanStatus.READY:
            return AgentResponse(
                ok=True,
                question=request.question,
                contextualized_question=contextualized_question,
                conversation_id=request.conversation_id,
                tenant_id=principal.tenant_id,
                dataset_query_plan=serialized_plan,
                message_type="clarification",
                answer=plan.clarification_question,
                trace=(
                    AgentTraceEntry(
                        node="resolve_datasource",
                        status="completed",
                    ),
                    AgentTraceEntry(
                        node="plan_dataset_query",
                        status=plan.status.value,
                    ),
                )
                if request.include_trace
                else (),
            )
        dialect = connector.capabilities().dialect
        if dialect not in {"postgres", "sqlite", "duckdb"}:
            return self._failure(
                request,
                principal,
                ErrorCode.CONFIG_INVALID,
                "selected datasource connector is not available for this query path",
                "resolve_datasource",
            )
        bundle_digest = hashlib.sha256(
            (
                f"{source.source_id}:{snapshot.version}:"
                f"{binding.binding_id}:{binding.version}"
            ).encode()
        ).hexdigest()
        try:
            prepared = self._compiler.compile(
                plan=plan,
                binding=binding,
                dialect=dialect,
                schema_fingerprint=snapshot.fingerprint,
                bundle_digest=bundle_digest,
                catalog=snapshot.catalog,
            )
        except ValueError as exc:
            graph_code = next(
                (
                    code
                    for code in (
                        ErrorCode.GRAPH_NO_PATH,
                        ErrorCode.GRAPH_AMBIGUOUS_PATH,
                        ErrorCode.GRAPH_UNSAFE_FANOUT,
                    )
                    if str(exc).startswith(f"{code}:")
                ),
                None,
            )
            return self._failure(
                request,
                principal,
                graph_code or ErrorCode.SQL_COMPILE_ERROR,
                str(exc),
                "compile_dataset_query",
            )
        traces = [
            AgentTraceEntry(node="resolve_datasource", status="completed"),
            AgentTraceEntry(node="plan_dataset_query", status="completed"),
            AgentTraceEntry(node="compile_dataset_query", status="completed"),
        ]
        if request.mode == AgentMode.PLAN:
            return AgentResponse(
                ok=True,
                question=request.question,
                contextualized_question=contextualized_question,
                conversation_id=request.conversation_id,
                tenant_id=principal.tenant_id,
                logical_plan=prepared.logical_plan,
                dataset_query_plan=serialized_plan,
                sql=prepared.logical_sql,
                message_type="plan",
                answer="已生成只读查询计划，尚未执行。",
                trace=tuple(traces) if request.include_trace else (),
            )
        now = datetime.now(UTC)
        expires_at = now + timedelta(seconds=60)
        grant_id = "dataset-grant-" + uuid4().hex
        grant = AccessGrant(
            grant_id=grant_id,
            tool_name="query.execute",
            tool_version="1.0.0",
            skill_id="dataset.analytics",
            bundle_digest=bundle_digest,
            schema_fingerprint=snapshot.fingerprint,
            source=source.source_id,
            principal_user_id=principal.user_id,
            tenant_id=principal.tenant_id,
            admin_bypass=False,
            allowed_relations=prepared.allowed_relations,
            max_rows=prepared.max_rows,
            statement_timeout_ms=15_000,
            policy_decision_id=prepared.policy_decision_id,
            logical_plan_hash=prepared.logical_plan_hash,
            prepared_query_hash=prepared.sql_ast_hash,
            issued_at=now,
            expires_at=expires_at,
        )
        lease = CredentialLease(
            credential_id="dataset-lease-" + uuid4().hex,
            grant_id=grant_id,
            bundle_digest=bundle_digest,
            source=source.source_id,
            connection_ref=context.connection_ref,
            capabilities=("query.execute",),
            secret="internal-immutable-snapshot",
            issued_at=now,
            expires_at=expires_at,
        )
        try:
            if request.mode == AgentMode.PREVIEW:
                result = await connector.preview(
                    prepared,
                    grant,
                    lease,
                    preview_rows=min(20, prepared.max_rows),
                )
            else:
                result = await connector.execute_readonly(
                    prepared,
                    grant,
                    lease,
                )
        except ConnectorError as exc:
            code, message, retryable = self._connector_failure(exc.code)
            return self._failure(
                request,
                principal,
                code,
                message,
                "execute_dataset_query",
                retryable=retryable,
            )
        except Exception:
            return self._failure(
                request,
                principal,
                ErrorCode.INTERNAL_ERROR,
                "selected datasource query could not be executed safely",
                "execute_dataset_query",
            )
        traces.append(
            AgentTraceEntry(node="execute_dataset_query", status="completed")
        )
        rows = self._rows(result)
        chart = self._chart(
            result,
            plan=plan,
            title=request.question,
        )
        return AgentResponse(
            ok=True,
            question=request.question,
            contextualized_question=contextualized_question,
            conversation_id=request.conversation_id,
            tenant_id=principal.tenant_id,
            logical_plan=prepared.logical_plan,
            dataset_query_plan=serialized_plan,
            sql=prepared.logical_sql,
            message_type="chart" if chart is not None else "table",
            rows=rows,
            chart=chart,
            answer=self._answer(result, chart=chart),
            trace=tuple(traces) if request.include_trace else (),
        )

    async def stream(
        self,
        request: AgentRequest,
        principal: PrincipalContext,
        *,
        conversation_context: DatasetConversationContext | None = None,
    ) -> AsyncIterator[AgentEvent]:
        run_id = "dataset-run-" + uuid4().hex
        yield AgentEvent(
            type=AgentEventType.RUN_STARTED,
            run_id=run_id,
            sequence=0,
            data=RunStartedPayload(
                mode=request.mode,
                enterprise_id=request.enterprise_id,
                domain_id=request.domain_id,
            ),
        )
        try:
            response = await self.run(
                request,
                principal,
                conversation_context=conversation_context,
            )
        except asyncio.CancelledError:
            response = self._failure(
                request,
                principal,
                ErrorCode.CANCELLED,
                "用户数据源查询已取消。",
                "execute_dataset_query",
            )
        except Exception:
            response = self._failure(
                request,
                principal,
                ErrorCode.INTERNAL_ERROR,
                "用户数据源查询安全失败。",
                "execute_dataset_query",
            )
        if response.ok:
            yield AgentEvent(
                type=AgentEventType.RUN_COMPLETED,
                run_id=run_id,
                sequence=1,
                data=RunCompletedPayload(),
                response=response,
            )
            return
        error_code = (
            response.error.code
            if response.error is not None
            else ErrorCode.INTERNAL_ERROR
        )
        yield AgentEvent(
            type=AgentEventType.RUN_FAILED,
            run_id=run_id,
            sequence=1,
            data=RunFailedPayload(error_code=error_code),
            response=response,
        )

    @staticmethod
    def _rows(result: TabularResult) -> tuple[AgentRow, ...]:
        return tuple(
            AgentRow(
                root={
                    column: DataSourceQueryService._json_value(value)
                    for column, value in zip(
                        result.columns,
                        item.values,
                        strict=True,
                    )
                }
            )
            for item in result.rows
        )

    @staticmethod
    def _json_value(value):
        if value is None or isinstance(value, (str, int, bool)):
            return value
        if isinstance(value, float):
            return value if math.isfinite(value) else str(value)
        if isinstance(value, Decimal):
            return str(value)
        if isinstance(value, (date, datetime)):
            return value.isoformat()
        return str(value)

    @staticmethod
    def _chart(
        result: TabularResult,
        *,
        plan: DatasetQueryPlan,
        title: str,
    ) -> ChartSpec | None:
        if (
            plan.analysis_type != "aggregate"
            or not plan.group_by
            or len(result.columns) < 2
            or not result.rows
        ):
            return None
        x_field = result.columns[0]
        for index, y_field in enumerate(result.columns[1:], start=1):
            if any(
                isinstance(row.values[index], (int, float, Decimal))
                and not isinstance(row.values[index], bool)
                for row in result.rows
            ):
                return ChartSpec(
                    title=title,
                    x_field=x_field,
                    y_field=y_field,
                )
        return None

    @staticmethod
    def _answer(
        result: TabularResult,
        *,
        chart: ChartSpec | None,
    ) -> str:
        if chart is None or not result.rows:
            return f"查询完成，共返回 {len(result.rows)} 行。"
        x_index = result.columns.index(chart.x_field)
        y_index = result.columns.index(chart.y_field)
        numeric = [
            (row.values[x_index], row.values[y_index])
            for row in result.rows
            if isinstance(row.values[y_index], (int, float, Decimal))
            and not isinstance(row.values[y_index], bool)
        ]
        if not numeric:
            return f"查询完成，共返回 {len(result.rows)} 行。"
        label, value = max(numeric, key=lambda item: float(item[1]))
        return (
            f"在本次返回结果中，{chart.x_field}={label} 的 "
            f"{chart.y_field} 最高，为 {value}；共返回 {len(result.rows)} 行。"
        )

    @staticmethod
    def _failure(
        request: AgentRequest,
        principal: PrincipalContext,
        code: ErrorCode,
        message: str,
        node: str,
        *,
        retryable: bool = False,
    ) -> AgentResponse:
        safe_message = message.strip()[:1000] or "dataset query failed"
        return AgentResponse(
            ok=False,
            question=request.question,
            contextualized_question=request.question,
            conversation_id=request.conversation_id,
            tenant_id=principal.tenant_id,
            answer=safe_message,
            error=AgentError(
                code=code,
                message=safe_message,
                retryable=retryable,
            ),
            trace=(
                AgentTraceEntry(
                    node=node,
                    status="failed",
                    error_code=code.value,
                ),
            )
            if request.include_trace
            else (),
        )

    @staticmethod
    def _connector_failure(
        code: ConnectorErrorCode,
    ) -> tuple[ErrorCode, str, bool]:
        if code in {
            ConnectorErrorCode.CREDENTIAL_EXPIRED,
            ConnectorErrorCode.CREDENTIAL_MISMATCH,
            ConnectorErrorCode.GRANT_EXPIRED,
            ConnectorErrorCode.GRANT_MISMATCH,
        }:
            return (
                ErrorCode.ACCESS_DENIED,
                "数据源执行授权无效或已过期。",
                False,
            )
        if code == ConnectorErrorCode.RELATION_NOT_ALLOWED:
            return (
                ErrorCode.SQL_POLICY_VIOLATION,
                "查询引用了未授权的数据表。",
                False,
            )
        if code == ConnectorErrorCode.ROW_LIMIT_EXCEEDED:
            return (
                ErrorCode.COST_EXCEEDED,
                "查询结果超过允许的行数上限。",
                False,
            )
        if code == ConnectorErrorCode.TIMEOUT:
            return (
                ErrorCode.DEADLINE_EXCEEDED,
                "数据源查询超时，请缩小查询范围后重试。",
                True,
            )
        if code == ConnectorErrorCode.CATALOG_INVALID:
            return (
                ErrorCode.BINDING_STALE,
                "数据源结构已变化，请刷新目录并重新激活语义绑定。",
                False,
            )
        return (
            ErrorCode.INTERNAL_ERROR,
            "数据源当前不可用，请稍后重试。",
            True,
        )


__all__ = [
    "DataSourceQueryService",
    "DatasetLogicalPlanner",
    "DatasetConversationContext",
    "DatasetPlanPatch",
    "DatasetPlanStatus",
    "DatasetPlanUpdate",
    "DatasetQueryCompiler",
    "DatasetQueryPlan",
]
