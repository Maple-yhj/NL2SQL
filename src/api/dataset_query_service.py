"""Governed query path for user-selected single-relation datasets."""

from __future__ import annotations

import hashlib
import json
import math
import re
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
)
from data_agent.runtime.binding import PreparedQuery, QueryParameter
from data_agent.runtime.dependencies import ModelClient
from data_agent.runtime.errors import AgentError, ErrorCode
from data_agent.runtime.models import (
    AgentMode,
    AgentRequest,
    AgentResponse,
    AgentRow,
    AgentTraceEntry,
    ChartSpec,
    PrincipalContext,
)
from data_agent.skills.models import AnalysisType, LogicalQueryPlan, ResultShape
from data_agent.tools import AccessGrant, CredentialLease
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
    "matching the supplied DatasetQueryPlan JSON Schema. Never return SQL or "
    "physical table/column names. Use only logical refs from logicalCatalog. "
    "Prefer a small result and never exceed the schema limit."
)
_FENCED_JSON = re.compile(
    r"```(?:json)?\s*(\{.*\})\s*```",
    re.DOTALL | re.IGNORECASE,
)


class DatasetPlanModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


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
    analysis_type: Literal["detail", "aggregate"]
    select: tuple[NonBlankText, ...] = ()
    aggregations: tuple[DatasetAggregation, ...] = ()
    group_by: tuple[NonBlankText, ...] = ()
    filters: tuple[DatasetFilter, ...] = ()
    order_by: tuple[DatasetOrdering, ...] = ()
    limit: int = Field(default=100, ge=1, le=1000)

    @model_validator(mode="after")
    def validate_shape(self) -> "DatasetQueryPlan":
        if self.analysis_type == "detail":
            if not self.select or self.aggregations or self.group_by:
                raise ValueError("detail plans require select fields only")
        elif not self.aggregations:
            raise ValueError("aggregate plans require aggregations")
        aliases = tuple(item.alias for item in self.aggregations)
        if len(aliases) != len(set(aliases)):
            raise ValueError("aggregation aliases must be unique")
        for values in (self.select, self.group_by):
            if len(values) != len(set(values)):
                raise ValueError("logical refs must be unique")
        return self


class DatasetLogicalPlanner:
    def __init__(self, model_client: ModelClient, *, max_attempts: int = 2) -> None:
        self._model_client = model_client
        self._max_attempts = max_attempts

    async def build_plan(
        self,
        *,
        question: str,
        binding: SemanticBindingRecord,
        catalog: CatalogSnapshot,
    ) -> DatasetQueryPlan:
        type_by_physical = {
            (relation.relation, column.name): column.data_type
            for relation in catalog.relations
            for column in relation.columns
        }
        logical_catalog = [
            {
                "ref": mapping.logical_ref,
                "type": type_by_physical.get(
                    (mapping.physical_relation, mapping.physical_column),
                    "unknown",
                ),
            }
            for mapping in binding.mappings
        ]
        request = {
            "task": "create_dataset_query_plan",
            "question": question,
            "logicalCatalog": logical_catalog,
            "datasetQueryPlanSchema": DatasetQueryPlan.model_json_schema(),
        }
        prompt = json.dumps(request, ensure_ascii=False, separators=(",", ":"))
        failure = ""
        for attempt in range(self._max_attempts):
            raw = await self._model_client.complete(
                prompt,
                system=_SYSTEM_PROMPT,
                max_output_tokens=2048,
            )
            try:
                return DatasetQueryPlan.model_validate(self._json_object(raw))
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
        binding: SemanticBindingRecord,
        dialect: Literal["postgres", "sqlite", "duckdb"],
        schema_fingerprint: str,
        bundle_digest: str,
    ) -> PreparedQuery:
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
        if len(relations) != 1:
            raise ValueError(
                "the first dataset runtime version supports one relation per query"
            )
        relation = next(iter(relations))
        if relation.count(".") != 1:
            raise ValueError("physical relation must be schema-qualified")
        schema, table = relation.split(".", 1)
        table_alias = "dataset"
        source = exp.Table(
            this=exp.to_identifier(table, quoted=True),
            db=exp.to_identifier(schema, quoted=True),
            alias=exp.TableAlias(
                this=exp.to_identifier(table_alias, quoted=True)
            ),
        )
        parameters: list[QueryParameter] = []

        def column(ref: str) -> exp.Column:
            mapping = mappings[ref]
            return exp.Column(
                this=exp.to_identifier(mapping.physical_column, quoted=True),
                table=exp.to_identifier(table_alias, quoted=True),
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
            allowed_relations=(relation,),
            policy_decision_id=hashlib.sha256(
                f"{binding.binding_id}:{binding.version}".encode()
            ).hexdigest(),
            estimated_cost=0,
            max_rows=plan.limit,
            bundle_digest=bundle_digest,
            schema_fingerprint=schema_fingerprint,
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
    ) -> AgentResponse:
        if (
            request.source_id is None
            or request.source_version is None
            or request.binding_id is None
            or request.binding_version is None
        ):
            raise ValueError("dataset query requires complete datasource pins")
        try:
            source, snapshot, binding, connector = (
                await self._data_sources.resolve_active_binding(
                    tenant_id=principal.tenant_id,
                    source_id=request.source_id,
                    source_version=request.source_version,
                    binding_id=request.binding_id,
                    binding_version=request.binding_version,
                    domain_id=request.domain_id,
                )
            )
        except DataSourceRegistryError as exc:
            return self._failure(
                request,
                principal,
                ErrorCode.BINDING_STALE,
                str(exc),
                "resolve_datasource",
            )
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
            plan = await self._planner.build_plan(
                question=request.question,
                binding=binding,
                catalog=snapshot.catalog,
            )
        except ValueError as exc:
            return self._failure(
                request,
                principal,
                ErrorCode.LOGICAL_PLAN_INVALID,
                str(exc),
                "plan_dataset_query",
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
            )
        except ValueError as exc:
            return self._failure(
                request,
                principal,
                ErrorCode.SQL_COMPILE_ERROR,
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
                contextualized_question=request.question,
                conversation_id=request.conversation_id,
                tenant_id=principal.tenant_id,
                logical_plan=prepared.logical_plan,
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
            connection_ref=source.location_ref or source.source_id,
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
            contextualized_question=request.question,
            conversation_id=request.conversation_id,
            tenant_id=principal.tenant_id,
            logical_plan=prepared.logical_plan,
            sql=prepared.logical_sql,
            message_type="chart" if chart is not None else "table",
            rows=rows,
            chart=chart,
            answer=self._answer(result, chart=chart),
            trace=tuple(traces) if request.include_trace else (),
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
    ) -> AgentResponse:
        safe_message = message.strip()[:1000] or "dataset query failed"
        return AgentResponse(
            ok=False,
            question=request.question,
            contextualized_question=request.question,
            conversation_id=request.conversation_id,
            tenant_id=principal.tenant_id,
            answer=safe_message,
            error=AgentError(code=code, message=safe_message),
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


__all__ = [
    "DataSourceQueryService",
    "DatasetLogicalPlanner",
    "DatasetQueryCompiler",
    "DatasetQueryPlan",
]
