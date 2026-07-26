"""Deterministic Commerce semantic binding and PostgreSQL AST compilation."""

from __future__ import annotations

import hashlib
from enum import StrEnum
from functools import reduce
from typing import Annotated, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)
from sqlglot import exp, parse

from data_agent.skills.models import (
    CalculationOperation,
    LogicalFilter,
    LogicalQueryPlan,
)
from data_agent.skills.validation import CommercePlanValidator, PlanValidationError

from .composition import ResolvedRuntimeBundle, stable_digest
from .models import PrincipalContext
from .packs import DomainPack, EnterpriseDataBinding, PhysicalFieldBinding
from .policy import compute_policy_decision_id


NonBlankText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
ParameterScalar = str | int | float | bool
SqlDialect = Literal["postgres", "sqlite", "duckdb"]


class BindingModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)

    def model_copy(
        self,
        *,
        update: dict[str, object] | None = None,
        deep: bool = False,
    ) -> Self:
        if not update:
            return super().model_copy(deep=deep)
        fields = type(self).model_fields
        unknown = set(update) - set(fields)
        if unknown:
            raise ValueError(
                "model_copy update contains unknown fields: "
                + ", ".join(sorted(unknown))
            )
        values = {name: getattr(self, name) for name in fields}
        values.update(update)
        return type(self).model_validate(values)


class BindingErrorCode(StrEnum):
    BUNDLE_MISMATCH = "BUNDLE_MISMATCH"
    LOGICAL_PLAN_INVALID = "LOGICAL_PLAN_INVALID"
    UNKNOWN_REFERENCE = "UNKNOWN_REFERENCE"
    UNKNOWN_RELATION = "UNKNOWN_RELATION"
    DISCONNECTED_PLAN = "DISCONNECTED_PLAN"
    POLICY_INVALID = "POLICY_INVALID"
    SQL_COMPILE_ERROR = "SQL_COMPILE_ERROR"
    BOUND_PLAN_MISMATCH = "BOUND_PLAN_MISMATCH"


class BindingError(ValueError):
    def __init__(self, code: BindingErrorCode, message: str) -> None:
        self.code = code
        super().__init__(message)


class BoundEntity(BindingModel):
    canonical_entity: NonBlankText
    physical_relation: NonBlankText
    alias: NonBlankText


class BoundSelection(BindingModel):
    logical_ref: NonBlankText
    alias: NonBlankText
    kind: Literal["dimension", "field", "metric", "calculation"]
    physical_relation: NonBlankText | None = None
    physical_column: NonBlankText | None = None


class BoundJoin(BindingModel):
    relationship_ref: NonBlankText
    from_entity: NonBlankText
    to_entity: NonBlankText
    from_relation: NonBlankText
    to_relation: NonBlankText
    from_columns: tuple[NonBlankText, ...] = Field(min_length=1)
    to_columns: tuple[NonBlankText, ...] = Field(min_length=1)


class BoundPredicate(BindingModel):
    logical_ref: NonBlankText
    operator: NonBlankText
    value: ParameterScalar | tuple[ParameterScalar, ...] | None = None
    purpose: Literal["filter", "time_start", "time_end", "tenant_context", "tenant_scope"]
    policy_enforced: bool = False


class BoundAggregation(BindingModel):
    output_ref: NonBlankText
    operation: NonBlankText
    input_refs: tuple[NonBlankText, ...] = Field(min_length=1)


class BoundGrouping(BindingModel):
    logical_ref: NonBlankText
    time_grain: NonBlankText | None = None


class BoundOrdering(BindingModel):
    logical_ref: NonBlankText
    direction: Literal["asc", "desc"]


class BoundAlignmentProof(BindingModel):
    source_entity: NonBlankText
    target_entity: NonBlankText
    strategy: Literal["pre_aggregate", "distinct"]
    relationship_path: tuple[NonBlankText, ...] = Field(min_length=1)
    joins: tuple[BoundJoin, ...] = Field(min_length=1)
    join_grain: tuple[NonBlankText, ...] = Field(min_length=1)
    affected_outputs: tuple[NonBlankText, ...] = Field(min_length=1)


class BoundOwnershipGuard(BindingModel):
    anchor_entity: NonBlankText
    relationship_path: tuple[NonBlankText, ...] = Field(min_length=1)
    joins: tuple[BoundJoin, ...] = Field(min_length=1)
    terminal_scope_ref: NonBlankText
    tenant_value: ParameterScalar


class BindingLineage(BindingModel):
    domain_pack_digest: NonBlankText
    enterprise_binding_digest: NonBlankText
    bundle_digest: NonBlankText
    logical_refs: tuple[NonBlankText, ...]
    physical_relations: tuple[NonBlankText, ...]


class RequiredAccess(BindingModel):
    allowed_relations: tuple[NonBlankText, ...]
    tenant_scoped: bool
    admin_bypass: bool
    policy_decision_id: NonBlankText
    max_rows: int = Field(ge=1)
    statement_timeout_ms: int = Field(ge=1)


class BoundQueryPlan(BindingModel):
    logical_plan: LogicalQueryPlan
    logical_plan_hash: NonBlankText
    physical_relations: tuple[NonBlankText, ...] = Field(min_length=1)
    entities: tuple[BoundEntity, ...] = Field(min_length=1)
    selected_columns: tuple[BoundSelection, ...] = Field(min_length=1)
    joins: tuple[BoundJoin, ...] = ()
    predicates: tuple[BoundPredicate, ...] = ()
    aggregations: tuple[BoundAggregation, ...] = ()
    grouping: tuple[BoundGrouping, ...] = ()
    ordering: tuple[BoundOrdering, ...] = ()
    alignment_proofs: tuple[BoundAlignmentProof, ...] = ()
    ownership_guards: tuple[BoundOwnershipGuard, ...] = ()
    limit: int = Field(ge=1)
    lineage: BindingLineage
    required_access: RequiredAccess


class QueryParameter(BindingModel):
    position: int = Field(ge=1)
    value: ParameterScalar
    logical_type: NonBlankText | None = None
    purpose: Literal[
        "filter",
        "time_start",
        "time_end",
        "tenant_context",
        "tenant_scope",
        "binding_constant",
        "enum_mapping",
        "algorithm_constant",
        "limit",
    ]


def _statement_relations(statement: exp.Expression) -> tuple[str, ...]:
    relations: list[str] = []
    for table in statement.find_all(exp.Table):
        name = table.name
        database = table.db
        relation = f"{database}.{name}" if database else name
        if relation not in relations:
            relations.append(relation)
    return tuple(relations)


class PreparedQuery(BindingModel):
    dialect: SqlDialect = "postgres"
    logical_plan: LogicalQueryPlan
    logical_plan_hash: NonBlankText
    sql_ast_hash: NonBlankText
    logical_sql: NonBlankText
    executable_sql: NonBlankText
    parameters: tuple[QueryParameter, ...]
    allowed_relations: tuple[NonBlankText, ...] = Field(min_length=1)
    policy_decision_id: NonBlankText
    estimated_cost: float | None = Field(default=None, ge=0)
    max_rows: int = Field(ge=1)
    bundle_digest: NonBlankText
    schema_fingerprint: NonBlankText
    read_only: Literal[True] = True

    @model_validator(mode="after")
    def validate_readonly_statement(self) -> "PreparedQuery":
        try:
            statements = parse(self.executable_sql, read=self.dialect)
        except Exception as exc:  # sqlglot uses several parse exception types
            raise ValueError(
                f"prepared query is not valid {self.dialect} SQL"
            ) from exc
        if len(statements) != 1 or not isinstance(statements[0], exp.Select):
            raise ValueError("prepared query must contain one read-only SELECT")
        statement = statements[0]
        forbidden_types = (
            exp.Insert,
            exp.Update,
            exp.Delete,
            exp.Create,
            exp.Drop,
            exp.Alter,
            exp.Command,
        )
        if any(statement.find(node_type) is not None for node_type in forbidden_types):
            raise ValueError("prepared query contains a non-read-only operation")
        observed_relations = set(_statement_relations(statement))
        if not observed_relations or not observed_relations.issubset(
            set(self.allowed_relations)
        ):
            raise ValueError("prepared query references an unauthorized relation")
        positions = tuple(item.position for item in self.parameters)
        if positions != tuple(range(1, len(positions) + 1)):
            raise ValueError("prepared query parameters must be contiguous")
        placeholders = {
            int(item.this.this)
            for item in statement.find_all(exp.Parameter)
            if isinstance(item.this, exp.Literal) and str(item.this.this).isdigit()
        }
        placeholders.update(
            int(item.this)
            for item in statement.find_all(exp.Placeholder)
            if str(item.this).isdigit()
        )
        if placeholders != set(positions):
            raise ValueError("prepared query placeholders do not match parameters")
        expected_hash = hashlib.sha256(
            self.executable_sql.encode("utf-8")
        ).hexdigest()
        if self.sql_ast_hash != expected_hash:
            raise ValueError("prepared query AST hash does not match SQL")
        return self


_AGGREGATE_OPERATIONS = {
    CalculationOperation.SUM,
    CalculationOperation.AVERAGE,
    CalculationOperation.COUNT,
    CalculationOperation.COUNT_DISTINCT,
}


def _quoted_identifier(value: str) -> exp.Identifier:
    return exp.to_identifier(value, quoted=True)


def _table_expression(relation: str, alias: str) -> exp.Table:
    schema, table = relation.split(".", 1)
    return exp.Table(
        this=_quoted_identifier(table),
        db=_quoted_identifier(schema),
        alias=exp.TableAlias(this=_quoted_identifier(alias)),
    )


def _and_all(items: list[exp.Expression]) -> exp.Expression:
    if not items:
        raise ValueError("cannot combine an empty predicate list")
    return reduce(lambda left, right: exp.and_(left, right), items)


class BindingCompiler:
    """Bind only trusted packs/bundles, then compile only sqlglot AST nodes."""

    def __init__(
        self,
        domain_pack: DomainPack,
        enterprise_binding: EnterpriseDataBinding,
        bundle: ResolvedRuntimeBundle,
        *,
        dialect: SqlDialect = "postgres",
    ) -> None:
        if dialect not in {"postgres", "sqlite", "duckdb"}:
            raise ValueError("unsupported SQL dialect")
        self._domain = domain_pack
        self._enterprise = enterprise_binding
        self._bundle = bundle
        self._dialect = dialect
        self._validator = CommercePlanValidator()
        self._validate_authority()
        self._relationships = {
            item.name: item for item in self._domain.spec.relationships
        }
        self._calculations: dict[str, object] = {}

    def _validate_authority(self) -> None:
        checks = (
            stable_digest(self._domain) == self._bundle.domain_pack_digest,
            stable_digest(self._enterprise)
            == self._bundle.enterprise_binding_digest,
            stable_digest(self._domain.spec)
            == stable_digest(self._bundle.semantic_model),
            stable_digest(self._enterprise.spec.bindings)
            == stable_digest(self._bundle.physical_bindings),
            stable_digest(self._enterprise.spec.policies)
            == stable_digest(self._bundle.compiled_access_policy),
        )
        if not all(checks):
            raise BindingError(
                BindingErrorCode.BUNDLE_MISMATCH,
                "Domain, Enterprise Binding, and resolved bundle disagree",
            )
        policy_relations = set(
            self._enterprise.spec.policies.relation_allowlist
        )
        bundle_relations = set(
            self._bundle.compiled_access_policy.get("relationAllowlist", ())
        )
        if policy_relations != bundle_relations:
            raise BindingError(
                BindingErrorCode.BUNDLE_MISMATCH,
                "resolved relation authority disagrees with the enterprise policy",
            )

    def bind(
        self,
        logical_plan: LogicalQueryPlan,
        principal: PrincipalContext,
    ) -> BoundQueryPlan:
        try:
            self._validator.require_valid(logical_plan, self._domain)
        except PlanValidationError as exc:
            raise BindingError(
                BindingErrorCode.LOGICAL_PLAN_INVALID,
                "logical plan failed governed semantic validation",
            ) from exc

        policy = self._enterprise.spec.policies
        if policy.access_mode != "tenant_scoped":
            admin_bypass = True
            tenant_scoped = False
            ownership_paths: dict[str, tuple[str, ...]] = {}
        else:
            scope = policy.tenant_scope
            allowed_admin_roles = frozenset(scope.admin_bypass.allowed_roles)
            admin_bypass = bool(
                allowed_admin_roles.intersection(principal.roles)
            )
            tenant_scoped = not admin_bypass
            ownership_paths = scope.ownership_paths

        relationship_names = list(logical_plan.relationships)

        entities, joins = self._bind_join_tree(
            logical_plan.entities[0],
            tuple(relationship_names),
        )
        declared_entities = set(logical_plan.entities)
        bound_entities = {item.canonical_entity for item in entities}
        if not declared_entities.issubset(bound_entities):
            raise BindingError(
                BindingErrorCode.DISCONNECTED_PLAN,
                "logical plan entities do not form one bound join tree",
            )

        alignment_proofs = tuple(
            self._bind_alignment_proof(item, logical_plan)
            for item in logical_plan.grain_alignment
        )
        ownership_guards: tuple[BoundOwnershipGuard, ...] = ()
        if tenant_scoped and "commerce.Seller" not in bound_entities:
            anchor = logical_plan.entities[0]
            path = ownership_paths.get(anchor)
            if not path:
                raise BindingError(
                    BindingErrorCode.POLICY_INVALID,
                    "tenant ownership path is missing",
                )
            ownership_guards = (
                BoundOwnershipGuard(
                    anchor_entity=anchor,
                    relationship_path=path,
                    joins=tuple(self._bound_relationship(name) for name in path),
                    terminal_scope_ref="commerce.Seller.seller_id",
                    tenant_value=principal.tenant_id,
                ),
            )

        selections = self._bound_selections(logical_plan)
        predicates: list[BoundPredicate] = []
        for item in logical_plan.filters:
            value = item.value
            purpose: Literal["filter", "tenant_context"] = "filter"
            if value == "context.seller_id":
                value = principal.tenant_id
                purpose = "tenant_context"
            predicates.append(
                BoundPredicate(
                    logical_ref=item.ref,
                    operator=item.operator.value,
                    value=value,
                    purpose=purpose,
                )
            )
        if logical_plan.time_range is not None:
            if logical_plan.time_range.start is not None:
                predicates.append(
                    BoundPredicate(
                        logical_ref=logical_plan.time_range.field,
                        operator="gte",
                        value=logical_plan.time_range.start,
                        purpose="time_start",
                    )
                )
            if logical_plan.time_range.end is not None:
                predicates.append(
                    BoundPredicate(
                        logical_ref=logical_plan.time_range.field,
                        operator="lt",
                        value=logical_plan.time_range.end,
                        purpose="time_end",
                    )
                )
        if tenant_scoped and "commerce.Seller" in bound_entities:
            predicates.append(
                BoundPredicate(
                    logical_ref="commerce.Seller.seller_id",
                    operator="eq",
                    value=principal.tenant_id,
                    purpose="tenant_scope",
                    policy_enforced=True,
                )
            )

        aggregations: list[BoundAggregation] = []
        for metric_ref in logical_plan.metrics:
            metric = self._domain.spec.metrics[metric_ref]
            aggregations.append(
                BoundAggregation(
                    output_ref=metric_ref,
                    operation=metric.aggregation,
                    input_refs=metric.inputs,
                )
            )
        for calculation in logical_plan.derived_calculations:
            if self._calculation_is_aggregate(calculation.id, logical_plan):
                aggregations.append(
                    BoundAggregation(
                        output_ref=calculation.id,
                        operation=calculation.operation.value,
                        input_refs=calculation.inputs,
                    )
                )

        logical_hash = logical_plan.stable_hash()
        decision = compute_policy_decision_id(
            self._bundle,
            principal,
            logical_hash,
        )
        max_rows = min(
            policy.max_rows,
            int(self._bundle.runtime_limits.get("maxResultRows", policy.max_rows)),
        )
        effective_limit = min(logical_plan.limit or max_rows, max_rows)
        physical_relation_items = [item.physical_relation for item in entities]
        for guard in ownership_guards:
            for join in guard.joins:
                for relation in (join.from_relation, join.to_relation):
                    if relation not in physical_relation_items:
                        physical_relation_items.append(relation)
        physical_relations = tuple(physical_relation_items)
        logical_refs = tuple(
            dict.fromkeys(
                (
                    *(item.logical_ref for item in selections),
                    *(item.logical_ref for item in predicates),
                    *(item.output_ref for item in aggregations),
                )
            )
        )
        return BoundQueryPlan(
            logical_plan=logical_plan,
            logical_plan_hash=logical_hash,
            physical_relations=physical_relations,
            entities=entities,
            selected_columns=selections,
            joins=joins,
            predicates=tuple(predicates),
            aggregations=tuple(aggregations),
            grouping=tuple(
                BoundGrouping(
                    logical_ref=ref,
                    time_grain=(
                        logical_plan.time_grain.value
                        if logical_plan.time_grain is not None
                        and logical_plan.time_range is not None
                        and logical_plan.time_range.field == ref
                        else None
                    ),
                )
                for ref in logical_plan.dimensions
            ),
            ordering=tuple(
                BoundOrdering(
                    logical_ref=item.ref,
                    direction=item.direction.value,
                )
                for item in logical_plan.ordering
            ),
            alignment_proofs=alignment_proofs,
            ownership_guards=ownership_guards,
            limit=effective_limit,
            lineage=BindingLineage(
                domain_pack_digest=self._bundle.domain_pack_digest,
                enterprise_binding_digest=self._bundle.enterprise_binding_digest,
                bundle_digest=self._bundle.digest,
                logical_refs=logical_refs,
                physical_relations=physical_relations,
            ),
            required_access=RequiredAccess(
                allowed_relations=physical_relations,
                tenant_scoped=tenant_scoped,
                admin_bypass=admin_bypass,
                policy_decision_id=decision,
                max_rows=max_rows,
                statement_timeout_ms=policy.query_timeout_seconds * 1000,
            ),
        )

    def _bound_relationship(self, name: str) -> BoundJoin:
        relationship = self._relationships.get(name)
        physical = self._enterprise.spec.relationships.get(name)
        if relationship is None or physical is None:
            raise BindingError(
                BindingErrorCode.UNKNOWN_RELATION,
                "logical relationship is not physically bound",
            )
        return BoundJoin(
            relationship_ref=name,
            from_entity=relationship.from_entity,
            to_entity=relationship.to_entity,
            from_relation=self._enterprise.spec.bindings[
                relationship.from_entity
            ].relation,
            to_relation=self._enterprise.spec.bindings[
                relationship.to_entity
            ].relation,
            from_columns=physical.from_columns,
            to_columns=physical.to_columns,
        )

    def _bind_alignment_proof(
        self,
        proof: object,
        plan: LogicalQueryPlan,
    ) -> BoundAlignmentProof:
        affected: list[str] = []
        for aggregation in (
            *plan.metrics,
            *(item.id for item in plan.derived_calculations),
        ):
            if proof.source_entity in self._reference_source_entities(aggregation, plan):
                affected.append(aggregation)
        if not affected:
            raise BindingError(
                BindingErrorCode.LOGICAL_PLAN_INVALID,
                "grain alignment proof does not protect an output",
            )
        return BoundAlignmentProof(
            source_entity=proof.source_entity,
            target_entity=proof.target_entity,
            strategy=proof.strategy,
            relationship_path=proof.relationship_path,
            joins=tuple(
                self._bound_relationship(name)
                for name in proof.relationship_path
            ),
            join_grain=proof.join_grain,
            affected_outputs=tuple(affected),
        )

    def _reference_source_entities(
        self,
        ref: str,
        plan: LogicalQueryPlan,
        visiting: frozenset[str] = frozenset(),
    ) -> set[str]:
        if ref.count(".") == 2:
            return {ref.rsplit(".", 1)[0]}
        metric = self._domain.spec.metrics.get(ref)
        if metric is not None:
            return {
                item.rsplit(".", 1)[0]
                for item in metric.inputs
            }
        calculations = {item.id: item for item in plan.derived_calculations}
        calculation = calculations.get(ref)
        if calculation is None or ref in visiting:
            return set()
        result: set[str] = set()
        for item in calculation.inputs:
            result.update(
                self._reference_source_entities(item, plan, visiting | {ref})
            )
        return result

    def _bind_join_tree(
        self,
        anchor_entity: str,
        relationship_names: tuple[str, ...],
    ) -> tuple[tuple[BoundEntity, ...], tuple[BoundJoin, ...]]:
        if anchor_entity not in self._enterprise.spec.bindings:
            raise BindingError(
                BindingErrorCode.UNKNOWN_REFERENCE,
                "logical anchor has no enterprise binding",
            )
        ordered_entities = [anchor_entity]
        joined = {anchor_entity}
        pending = list(dict.fromkeys(relationship_names))
        ordered_relationships: list[str] = []
        while pending:
            progressed = False
            for name in tuple(pending):
                relationship = self._relationships.get(name)
                physical = self._enterprise.spec.relationships.get(name)
                if relationship is None or physical is None:
                    raise BindingError(
                        BindingErrorCode.UNKNOWN_RELATION,
                        "logical relationship is not physically bound",
                    )
                from_known = relationship.from_entity in joined
                to_known = relationship.to_entity in joined
                if from_known and to_known:
                    pending.remove(name)
                    continue
                if from_known == to_known:
                    continue
                new_entity = (
                    relationship.to_entity if from_known else relationship.from_entity
                )
                joined.add(new_entity)
                ordered_entities.append(new_entity)
                ordered_relationships.append(name)
                pending.remove(name)
                progressed = True
            if not progressed and pending:
                raise BindingError(
                    BindingErrorCode.DISCONNECTED_PLAN,
                    "logical relationships do not connect to the plan anchor",
                )

        entities = tuple(
            BoundEntity(
                canonical_entity=entity,
                physical_relation=self._enterprise.spec.bindings[entity].relation,
                alias=f"t{index}",
            )
            for index, entity in enumerate(ordered_entities)
        )
        joins = tuple(
            BoundJoin(
                relationship_ref=name,
                from_entity=self._relationships[name].from_entity,
                to_entity=self._relationships[name].to_entity,
                from_relation=self._enterprise.spec.bindings[
                    self._relationships[name].from_entity
                ].relation,
                to_relation=self._enterprise.spec.bindings[
                    self._relationships[name].to_entity
                ].relation,
                from_columns=self._enterprise.spec.relationships[name].from_columns,
                to_columns=self._enterprise.spec.relationships[name].to_columns,
            )
            for name in ordered_relationships
        )
        return entities, joins

    def _bound_selections(
        self,
        plan: LogicalQueryPlan,
    ) -> tuple[BoundSelection, ...]:
        consumed_calculations = {
            ref
            for calculation in plan.derived_calculations
            for ref in calculation.inputs
            if ref.startswith(f"{self._domain.metadata.name}.")
        }
        leaf_calculations = tuple(
            item.id
            for item in plan.derived_calculations
            if item.id not in consumed_calculations
        )
        requested: list[tuple[str, Literal["dimension", "field", "metric", "calculation"]]] = [
            *((item, "dimension") for item in plan.dimensions),
            *((item, "field") for item in plan.fields),
            *((item, "metric") for item in plan.metrics),
            *((item, "calculation") for item in leaf_calculations),
        ]
        used_aliases: set[str] = set()
        selections: list[BoundSelection] = []
        for ref, kind in requested:
            base = ref.rsplit(".", 1)[-1]
            alias = base
            suffix = 2
            while alias in used_aliases:
                alias = f"{base}_{suffix}"
                suffix += 1
            used_aliases.add(alias)
            relation: str | None = None
            column: str | None = None
            if kind in {"dimension", "field"}:
                entity, field_name = ref.rsplit(".", 1)
                binding = self._enterprise.spec.bindings.get(entity)
                if binding is None or field_name not in binding.fields:
                    raise BindingError(
                        BindingErrorCode.UNKNOWN_REFERENCE,
                        "canonical field has no physical binding",
                    )
                relation = binding.relation
                column = binding.fields[field_name].column
            selections.append(
                BoundSelection(
                    logical_ref=ref,
                    alias=alias,
                    kind=kind,
                    physical_relation=relation,
                    physical_column=column,
                )
            )
        return tuple(selections)

    def _calculation_is_aggregate(
        self,
        calculation_id: str,
        plan: LogicalQueryPlan,
        visiting: frozenset[str] = frozenset(),
    ) -> bool:
        if calculation_id in self._domain.spec.metrics:
            return True
        calculations = {item.id: item for item in plan.derived_calculations}
        calculation = calculations.get(calculation_id)
        if calculation is None or calculation_id in visiting:
            return False
        if calculation.operation in _AGGREGATE_OPERATIONS:
            return True
        return any(
            self._calculation_is_aggregate(ref, plan, visiting | {calculation_id})
            for ref in calculation.inputs
        )

    def compile(
        self,
        bound_plan: BoundQueryPlan,
        principal: PrincipalContext,
    ) -> PreparedQuery:
        expected = self.bind(bound_plan.logical_plan, principal)
        if expected != bound_plan:
            raise BindingError(
                BindingErrorCode.BOUND_PLAN_MISMATCH,
                "bound query plan does not match trusted semantic binding",
            )
        if (
            bound_plan.lineage.bundle_digest != self._bundle.digest
            or bound_plan.lineage.domain_pack_digest
            != self._bundle.domain_pack_digest
            or bound_plan.lineage.enterprise_binding_digest
            != self._bundle.enterprise_binding_digest
        ):
            raise BindingError(
                BindingErrorCode.BUNDLE_MISMATCH,
                "bound query plan was produced by a different authority",
            )
        try:
            return self._compile_ast(bound_plan)
        except BindingError:
            raise
        except Exception as exc:
            raise BindingError(
                BindingErrorCode.SQL_COMPILE_ERROR,
                "could not compile the bound query plan",
            ) from exc

    def _compile_ast(self, bound: BoundQueryPlan) -> PreparedQuery:
        if bound.alignment_proofs:
            return self._compile_aligned_ast(bound)
        plan = bound.logical_plan
        calculations = {item.id: item for item in plan.derived_calculations}
        entity_aliases = {
            item.canonical_entity: item.alias for item in bound.entities
        }
        selection_aliases = {
            item.logical_ref: item.alias for item in bound.selected_columns
        }
        parameters: list[QueryParameter] = []
        algorithm_parameters: dict[tuple[ParameterScalar, str | None], exp.Parameter] = {}

        def parameter(
            value: ParameterScalar,
            purpose: QueryParameter.model_fields["purpose"].annotation,
            logical_type: str | None = None,
        ) -> exp.Parameter:
            position = len(parameters) + 1
            parameters.append(
                QueryParameter(
                    position=position,
                    value=value,
                    logical_type=logical_type,
                    purpose=purpose,
                )
            )
            return exp.Parameter(this=exp.Var(this=str(position)))

        def algorithm_parameter(
            value: ParameterScalar,
            logical_type: str | None = None,
        ) -> exp.Parameter:
            key = (value, logical_type)
            existing = algorithm_parameters.get(key)
            if existing is not None:
                return existing.copy()
            created = parameter(value, "algorithm_constant", logical_type)
            algorithm_parameters[key] = created
            return created.copy()

        def raw_field(ref: str) -> exp.Expression:
            try:
                entity, field_name = ref.rsplit(".", 1)
                canonical = self._domain.spec.entities[entity].fields[field_name]
                physical = self._enterprise.spec.bindings[entity].fields[field_name]
                alias = entity_aliases[entity]
            except (KeyError, ValueError) as exc:
                raise BindingError(
                    BindingErrorCode.UNKNOWN_REFERENCE,
                    "logical field cannot be resolved in the bound join tree",
                ) from exc
            if physical.value is not None:
                return parameter(
                    physical.value,
                    "binding_constant",
                    canonical.type,
                )
            if physical.column is None:
                raise BindingError(
                    BindingErrorCode.UNKNOWN_REFERENCE,
                    "physical field binding has no column or constant",
                )
            expression: exp.Expression = exp.Column(
                this=_quoted_identifier(physical.column),
                table=_quoted_identifier(alias),
            )
            expression = self._apply_cast(expression, physical)
            if physical.null_policy == "coalesce" and physical.coalesce_value is not None:
                expression = exp.Coalesce(
                    this=expression,
                    expressions=[
                        parameter(
                            physical.coalesce_value,
                            "binding_constant",
                            canonical.type,
                        )
                    ],
                )
            if physical.enum_mapping:
                cases: list[exp.If] = []
                for source, target in sorted(physical.enum_mapping.items()):
                    cases.append(
                        exp.If(
                            this=exp.EQ(
                                this=expression.copy(),
                                expression=parameter(
                                    source,
                                    "enum_mapping",
                                    canonical.type,
                                ),
                            ),
                            true=parameter(
                                target,
                                "enum_mapping",
                                canonical.type,
                            ),
                        )
                    )
                expression = exp.Case(
                    ifs=cases,
                    default=expression,
                )
            return expression

        def dimension(ref: str) -> exp.Expression:
            expression = raw_field(ref)
            if (
                plan.time_grain is not None
                and plan.time_range is not None
                and plan.time_range.field == ref
            ):
                return exp.func(
                    "DATE_TRUNC",
                    exp.Cast(
                        this=algorithm_parameter(plan.time_grain.value, "string"),
                        to=exp.DataType.build("TEXT", dialect="postgres"),
                    ),
                    expression,
                )
            return expression

        def metric(ref: str) -> exp.Expression:
            definition = self._domain.spec.metrics.get(ref)
            if definition is None:
                raise BindingError(
                    BindingErrorCode.UNKNOWN_REFERENCE,
                    "metric is not declared by the Domain Pack",
                )
            inputs = [raw_field(item) for item in definition.inputs]
            if definition.aggregation == "sum":
                pieces = [exp.Sum(this=item) for item in inputs]
                if definition.combine == "add":
                    return reduce(
                        lambda left, right: exp.Add(this=left, expression=right),
                        pieces,
                    )
                return pieces[0]
            if definition.aggregation == "average":
                return exp.Avg(this=inputs[0])
            if definition.aggregation == "count":
                return exp.Count(this=inputs[0])
            if definition.aggregation == "count_distinct":
                return exp.Count(
                    this=exp.Distinct(expressions=[inputs[0]])
                )
            function = {
                "min": exp.Min,
                "max": exp.Max,
            }.get(definition.aggregation)
            if function is None:
                raise BindingError(
                    BindingErrorCode.SQL_COMPILE_ERROR,
                    "metric aggregation is unsupported",
                )
            return function(this=inputs[0])

        def reference(ref: str) -> exp.Expression:
            if ref in self._domain.spec.metrics:
                return metric(ref)
            if ref in calculations:
                return calculation(ref)
            return raw_field(ref)

        def calculation(ref: str) -> exp.Expression:
            item = calculations.get(ref)
            if item is None:
                raise BindingError(
                    BindingErrorCode.UNKNOWN_REFERENCE,
                    "derived calculation is not declared",
                )
            inputs = [reference(input_ref) for input_ref in item.inputs]
            operation = item.operation
            if operation == CalculationOperation.SUM:
                return exp.Sum(this=inputs[0])
            if operation == CalculationOperation.AVERAGE:
                return exp.Avg(this=inputs[0])
            if operation == CalculationOperation.COUNT:
                return exp.Count(this=inputs[0])
            if operation == CalculationOperation.COUNT_DISTINCT:
                return exp.Count(
                    this=exp.Distinct(expressions=[inputs[0]])
                )
            if operation == CalculationOperation.ADD:
                return reduce(
                    lambda left, right: exp.Add(this=left, expression=right),
                    inputs,
                )
            if operation == CalculationOperation.SUBTRACT:
                return exp.Sub(this=inputs[0], expression=inputs[1])
            if operation == CalculationOperation.MULTIPLY:
                return reduce(
                    lambda left, right: exp.Mul(this=left, expression=right),
                    inputs,
                )
            if operation == CalculationOperation.DATE_DIFFERENCE:
                return exp.Div(
                    this=exp.func(
                        "DATE_PART",
                        exp.Cast(
                            this=algorithm_parameter("epoch", "string"),
                            to=exp.DataType.build("TEXT", dialect="postgres"),
                        ),
                        exp.Sub(this=inputs[0], expression=inputs[1]),
                    ),
                    expression=algorithm_parameter(86400, "decimal"),
                )
            if operation == CalculationOperation.COMPOSITE_KEY:
                return exp.Tuple(expressions=inputs)
            if operation in {
                CalculationOperation.GROWTH,
                CalculationOperation.LAG,
            }:
                window_spec = next(
                    (
                        window
                        for window in plan.window_specs
                        if window.calculation == item.id
                    ),
                    None,
                )
                if window_spec is None:
                    raise BindingError(
                        BindingErrorCode.SQL_COMPILE_ERROR,
                        "window calculation has no governed window specification",
                    )
                order_items = [
                    exp.Ordered(
                        this=dimension(order.ref),
                        desc=order.direction.value == "desc",
                    )
                    for order in window_spec.ordering
                ]

                def lagged() -> exp.Window:
                    return exp.Window(
                        this=exp.func("LAG", inputs[0].copy()),
                        partition_by=[
                            raw_field(partition_ref)
                            for partition_ref in window_spec.partition_by
                        ],
                        order=exp.Order(expressions=[item.copy() for item in order_items]),
                    )

                if operation == CalculationOperation.LAG:
                    return lagged()
                previous = lagged()
                return exp.Div(
                    this=exp.Sub(
                        this=inputs[0].copy(),
                        expression=previous.copy(),
                    ),
                    expression=exp.Nullif(
                        this=previous,
                        expression=algorithm_parameter(0, "decimal"),
                    ),
                )
            raise BindingError(
                BindingErrorCode.SQL_COMPILE_ERROR,
                "derived calculation operation is unsupported",
            )

        selected_expressions: list[exp.Expression] = []
        for selection in bound.selected_columns:
            if selection.kind == "dimension":
                value = dimension(selection.logical_ref)
            elif selection.kind == "field":
                value = raw_field(selection.logical_ref)
            elif selection.kind == "metric":
                value = metric(selection.logical_ref)
            else:
                value = calculation(selection.logical_ref)
            selected_expressions.append(
                exp.Alias(
                    this=value,
                    alias=_quoted_identifier(selection.alias),
                )
            )

        query = exp.Select(expressions=selected_expressions)
        anchor = bound.entities[0]
        query.set(
            "from_",
            exp.From(this=_table_expression(anchor.physical_relation, anchor.alias)),
        )
        aliases = {item.canonical_entity: item.alias for item in bound.entities}
        joined = {anchor.canonical_entity}
        joins: list[exp.Join] = []
        for item in bound.joins:
            if item.from_entity in joined and item.to_entity not in joined:
                new_entity = item.to_entity
            elif item.to_entity in joined and item.from_entity not in joined:
                new_entity = item.from_entity
            else:
                continue
            comparisons = [
                exp.EQ(
                    this=exp.Column(
                        this=_quoted_identifier(from_column),
                        table=_quoted_identifier(aliases[item.from_entity]),
                    ),
                    expression=exp.Column(
                        this=_quoted_identifier(to_column),
                        table=_quoted_identifier(aliases[item.to_entity]),
                    ),
                )
                for from_column, to_column in zip(
                    item.from_columns,
                    item.to_columns,
                    strict=True,
                )
            ]
            joins.append(
                exp.Join(
                    this=_table_expression(
                        self._enterprise.spec.bindings[new_entity].relation,
                        aliases[new_entity],
                    ),
                    on=_and_all(comparisons),
                    kind="INNER",
                )
            )
            joined.add(new_entity)
        query.set("joins", joins)

        def predicate_expression(item: BoundPredicate) -> exp.Expression:
            left = raw_field(item.logical_ref)
            logical_type: str | None = None
            if item.logical_ref.count(".") == 2:
                entity, field_name = item.logical_ref.rsplit(".", 1)
                field = self._domain.spec.entities.get(entity)
                if field is not None and field_name in field.fields:
                    logical_type = field.fields[field_name].type
            operator = item.operator
            if operator == "is_null":
                return exp.Is(this=left, expression=exp.Null())
            if operator == "is_not_null":
                return exp.Not(this=exp.Is(this=left, expression=exp.Null()))
            if isinstance(item.value, tuple):
                values = [
                    parameter(value, item.purpose, logical_type)
                    for value in item.value
                ]
                contained = exp.In(this=left, expressions=values)
                return exp.Not(this=contained) if operator == "not_in" else contained
            if item.value is None:
                raise BindingError(
                    BindingErrorCode.SQL_COMPILE_ERROR,
                    "non-null predicate has no parameter value",
                )
            right_value: ParameterScalar = item.value
            if operator == "contains":
                right_value = f"%{right_value}%"
            right = parameter(right_value, item.purpose, logical_type)
            operators: dict[str, type[exp.Binary]] = {
                "eq": exp.EQ,
                "neq": exp.NEQ,
                "gt": exp.GT,
                "gte": exp.GTE,
                "lt": exp.LT,
                "lte": exp.LTE,
                "contains": exp.Like,
            }
            binary = operators.get(operator)
            if binary is None:
                raise BindingError(
                    BindingErrorCode.SQL_COMPILE_ERROR,
                    "predicate operator is unsupported",
                )
            return binary(this=left, expression=right)

        where_items: list[exp.Expression] = []
        having_items: list[exp.Expression] = []
        aggregate_refs = set(plan.metrics) | {
            item.id
            for item in plan.derived_calculations
            if self._calculation_is_aggregate(item.id, plan)
        }
        for predicate in bound.predicates:
            if predicate.logical_ref in aggregate_refs:
                # Bound filters on aggregate aliases are governed as HAVING.
                synthetic = LogicalFilter(
                    ref=predicate.logical_ref,
                    operator=predicate.operator,
                    value=predicate.value,
                )
                left = reference(synthetic.ref)
                value = synthetic.value
                if value is None:
                    expression = (
                        exp.Is(this=left, expression=exp.Null())
                        if synthetic.operator.value == "is_null"
                        else exp.Not(this=exp.Is(this=left, expression=exp.Null()))
                    )
                elif isinstance(value, tuple):
                    expression = exp.In(
                        this=left,
                        expressions=[
                            parameter(item, predicate.purpose) for item in value
                        ],
                    )
                    if synthetic.operator.value == "not_in":
                        expression = exp.Not(this=expression)
                else:
                    right = parameter(value, predicate.purpose)
                    binary = {
                        "eq": exp.EQ,
                        "neq": exp.NEQ,
                        "gt": exp.GT,
                        "gte": exp.GTE,
                        "lt": exp.LT,
                        "lte": exp.LTE,
                    }[synthetic.operator.value]
                    expression = binary(this=left, expression=right)
                having_items.append(expression)
            elif predicate.logical_ref in calculations:
                left = calculation(predicate.logical_ref)
                if predicate.value is None:
                    expression = (
                        exp.Is(this=left, expression=exp.Null())
                        if predicate.operator == "is_null"
                        else exp.Not(this=exp.Is(this=left, expression=exp.Null()))
                    )
                else:
                    if isinstance(predicate.value, tuple):
                        expression = exp.In(
                            this=left,
                            expressions=[
                                parameter(item, predicate.purpose)
                                for item in predicate.value
                            ],
                        )
                    else:
                        right = parameter(predicate.value, predicate.purpose)
                        binary = {
                            "eq": exp.EQ,
                            "neq": exp.NEQ,
                            "gt": exp.GT,
                            "gte": exp.GTE,
                            "lt": exp.LT,
                            "lte": exp.LTE,
                        }[predicate.operator]
                        expression = binary(this=left, expression=right)
                if predicate.logical_ref in aggregate_refs:
                    having_items.append(expression)
                else:
                    where_items.append(expression)
            else:
                where_items.append(predicate_expression(predicate))

        for guard in bound.ownership_guards:
            where_items.append(
                self._ownership_exists_expression(
                    guard,
                    entity_aliases,
                    parameter,
                )
            )

        for predicate in plan.having:
            left = reference(predicate.ref)
            if predicate.value is None:
                expression = (
                    exp.Is(this=left, expression=exp.Null())
                    if predicate.operator.value == "is_null"
                    else exp.Not(this=exp.Is(this=left, expression=exp.Null()))
                )
            elif isinstance(predicate.value, tuple):
                expression = exp.In(
                    this=left,
                    expressions=[
                        parameter(item, "filter") for item in predicate.value
                    ],
                )
                if predicate.operator.value == "not_in":
                    expression = exp.Not(this=expression)
            else:
                right = parameter(predicate.value, "filter")
                binary = {
                    "eq": exp.EQ,
                    "neq": exp.NEQ,
                    "gt": exp.GT,
                    "gte": exp.GTE,
                    "lt": exp.LT,
                    "lte": exp.LTE,
                }[predicate.operator.value]
                expression = binary(this=left, expression=right)
            having_items.append(expression)
        if where_items:
            query.set("where", exp.Where(this=_and_all(where_items)))
        if bound.grouping and aggregate_refs:
            query.set(
                "group",
                exp.Group(
                    expressions=[
                        dimension(item.logical_ref) for item in bound.grouping
                    ]
                ),
            )
        if having_items:
            query.set("having", exp.Having(this=_and_all(having_items)))
        if bound.ordering:
            ordered: list[exp.Ordered] = []
            for item in bound.ordering:
                alias = selection_aliases.get(item.logical_ref)
                expression = (
                    exp.Column(this=_quoted_identifier(alias))
                    if alias is not None
                    else reference(item.logical_ref)
                )
                ordered.append(
                    exp.Ordered(
                        this=expression,
                        desc=item.direction == "desc",
                    )
                )
            query.set("order", exp.Order(expressions=ordered))
        limit_expression = parameter(bound.limit, "limit", "integer")
        query.set("limit", exp.Limit(expression=limit_expression))

        sql = query.sql(dialect=self._dialect, pretty=False)
        sql_hash = hashlib.sha256(sql.encode("utf-8")).hexdigest()
        return PreparedQuery(
            dialect=self._dialect,
            logical_plan=bound.logical_plan,
            logical_plan_hash=bound.logical_plan_hash,
            sql_ast_hash=sql_hash,
            logical_sql=sql,
            executable_sql=sql,
            parameters=tuple(parameters),
            allowed_relations=bound.physical_relations,
            policy_decision_id=bound.required_access.policy_decision_id,
            estimated_cost=0.0,
            max_rows=bound.limit,
            bundle_digest=self._bundle.digest,
            schema_fingerprint=self._bundle.schema_fingerprint,
        )

    def _ownership_exists_expression(
        self,
        guard: BoundOwnershipGuard,
        outer_aliases: dict[str, str],
        parameter: object,
    ) -> exp.Exists:
        current_entity = guard.anchor_entity
        current_alias = outer_aliases[current_entity]
        joins: list[exp.Join] = []
        correlation: exp.Expression | None = None
        local_aliases: dict[str, str] = {}
        from_table: exp.Table | None = None
        for index, relationship in enumerate(guard.joins):
            if relationship.from_entity == current_entity:
                next_entity = relationship.to_entity
            elif relationship.to_entity == current_entity:
                next_entity = relationship.from_entity
            else:
                raise BindingError(
                    BindingErrorCode.POLICY_INVALID,
                    "ownership path is disconnected",
                )
            next_alias = f"scope_{index}"
            local_aliases[next_entity] = next_alias
            aliases = {**local_aliases, current_entity: current_alias}
            comparisons = [
                exp.EQ(
                    this=exp.Column(
                        this=_quoted_identifier(from_column),
                        table=_quoted_identifier(aliases[relationship.from_entity]),
                    ),
                    expression=exp.Column(
                        this=_quoted_identifier(to_column),
                        table=_quoted_identifier(aliases[relationship.to_entity]),
                    ),
                )
                for from_column, to_column in zip(
                    relationship.from_columns,
                    relationship.to_columns,
                    strict=True,
                )
            ]
            condition = _and_all(comparisons)
            table = _table_expression(
                self._enterprise.spec.bindings[next_entity].relation,
                next_alias,
            )
            if index == 0:
                from_table = table
                correlation = condition
            else:
                joins.append(exp.Join(this=table, on=condition, kind="INNER"))
            current_entity = next_entity
            current_alias = next_alias
        if from_table is None or correlation is None or current_entity != "commerce.Seller":
            raise BindingError(
                BindingErrorCode.POLICY_INVALID,
                "ownership path does not terminate at Seller",
            )
        seller_column = self._enterprise.spec.bindings["commerce.Seller"].fields[
            "seller_id"
        ].column
        if seller_column is None:
            raise BindingError(
                BindingErrorCode.POLICY_INVALID,
                "seller scope field has no physical column",
            )
        tenant = parameter(guard.tenant_value, "tenant_scope", "string")
        seller_ref = exp.Column(
            this=_quoted_identifier(seller_column),
            table=_quoted_identifier(current_alias),
        )
        query = exp.Select(expressions=[seller_ref.copy()])
        query.set("from_", exp.From(this=from_table))
        query.set("joins", joins)
        query.set(
            "where",
            exp.Where(
                this=exp.and_(
                    correlation,
                    exp.EQ(this=seller_ref, expression=tenant),
                )
            ),
        )
        return exp.Exists(this=query)

    def _compile_aligned_ast(self, bound: BoundQueryPlan) -> PreparedQuery:
        """Lower grain proofs as a DISTINCT fact-grain association subquery.

        The inner relation keeps every protected source grain key and every
        semantic output/input field, then removes duplicates introduced by an
        expanding relationship. Aggregates run only over those governed rows.
        """

        plan = bound.logical_plan
        source_entities = {
            proof.source_entity for proof in bound.alignment_proofs
        }
        if len(source_entities) != 1:
            raise BindingError(
                BindingErrorCode.SQL_COMPILE_ERROR,
                "one aligned query must have one protected fact source",
            )
        calculations = {item.id: item for item in plan.derived_calculations}
        entity_aliases = {
            item.canonical_entity: item.alias for item in bound.entities
        }
        selection_aliases = {
            item.logical_ref: item.alias for item in bound.selected_columns
        }
        parameters: list[QueryParameter] = []
        algorithm_parameters: dict[tuple[ParameterScalar, str | None], exp.Parameter] = {}

        def parameter(
            value: ParameterScalar,
            purpose: QueryParameter.model_fields["purpose"].annotation,
            logical_type: str | None = None,
        ) -> exp.Parameter:
            position = len(parameters) + 1
            parameters.append(
                QueryParameter(
                    position=position,
                    value=value,
                    logical_type=logical_type,
                    purpose=purpose,
                )
            )
            return exp.Parameter(this=exp.Var(this=str(position)))

        def algorithm_parameter(
            value: ParameterScalar,
            logical_type: str | None = None,
        ) -> exp.Parameter:
            key = (value, logical_type)
            if key not in algorithm_parameters:
                algorithm_parameters[key] = parameter(
                    value,
                    "algorithm_constant",
                    logical_type,
                )
            return algorithm_parameters[key].copy()

        def physical_field(ref: str) -> exp.Expression:
            try:
                entity, field_name = ref.rsplit(".", 1)
                canonical = self._domain.spec.entities[entity].fields[field_name]
                physical = self._enterprise.spec.bindings[entity].fields[field_name]
                alias = entity_aliases[entity]
            except (KeyError, ValueError) as exc:
                raise BindingError(
                    BindingErrorCode.UNKNOWN_REFERENCE,
                    "aligned field is outside the semantic join tree",
                ) from exc
            if physical.value is not None:
                return parameter(
                    physical.value,
                    "binding_constant",
                    canonical.type,
                )
            if physical.column is None:
                raise BindingError(
                    BindingErrorCode.UNKNOWN_REFERENCE,
                    "aligned physical field has no column",
                )
            value: exp.Expression = exp.Column(
                this=_quoted_identifier(physical.column),
                table=_quoted_identifier(alias),
            )
            value = self._apply_cast(value, physical)
            if physical.null_policy == "coalesce" and physical.coalesce_value is not None:
                value = exp.Coalesce(
                    this=value,
                    expressions=[
                        parameter(
                            physical.coalesce_value,
                            "binding_constant",
                            canonical.type,
                        )
                    ],
                )
            return value

        raw_refs: list[str] = []

        def add_field(ref: str) -> None:
            if ref not in raw_refs:
                raw_refs.append(ref)

        def collect(ref: str, visiting: frozenset[str] = frozenset()) -> None:
            if ref.count(".") == 2:
                add_field(ref)
                return
            metric_definition = self._domain.spec.metrics.get(ref)
            if metric_definition is not None:
                for item in metric_definition.inputs:
                    collect(item, visiting)
                return
            calculation_definition = calculations.get(ref)
            if calculation_definition is None or ref in visiting:
                return
            for item in calculation_definition.inputs:
                collect(item, visiting | {ref})

        for source_entity in sorted(source_entities):
            source = self._domain.spec.entities[source_entity]
            for field_name in source.grain:
                add_field(f"{source_entity}.{field_name}")
        for ref in (*plan.dimensions, *plan.fields, *plan.metrics):
            collect(ref)
        for calculation_item in plan.derived_calculations:
            collect(calculation_item.id)
        for predicate_item in (*plan.filters, *plan.having):
            collect(predicate_item.ref)
        if plan.time_range is not None:
            collect(plan.time_range.field)
        for proof in bound.alignment_proofs:
            for ref in proof.join_grain:
                collect(ref)

        aligned_columns = {
            ref: f"r{index}" for index, ref in enumerate(raw_refs)
        }
        inner = exp.Select(
            expressions=[
                exp.Alias(
                    this=physical_field(ref),
                    alias=_quoted_identifier(aligned_columns[ref]),
                )
                for ref in raw_refs
            ]
        )
        inner.set("distinct", exp.Distinct())
        anchor = bound.entities[0]
        inner.set(
            "from_",
            exp.From(this=_table_expression(anchor.physical_relation, anchor.alias)),
        )
        joined = {anchor.canonical_entity}
        inner_joins: list[exp.Join] = []
        for item in bound.joins:
            if item.from_entity in joined and item.to_entity not in joined:
                new_entity = item.to_entity
            elif item.to_entity in joined and item.from_entity not in joined:
                new_entity = item.from_entity
            else:
                continue
            comparisons = [
                exp.EQ(
                    this=exp.Column(
                        this=_quoted_identifier(from_column),
                        table=_quoted_identifier(entity_aliases[item.from_entity]),
                    ),
                    expression=exp.Column(
                        this=_quoted_identifier(to_column),
                        table=_quoted_identifier(entity_aliases[item.to_entity]),
                    ),
                )
                for from_column, to_column in zip(
                    item.from_columns,
                    item.to_columns,
                    strict=True,
                )
            ]
            inner_joins.append(
                exp.Join(
                    this=_table_expression(
                        self._enterprise.spec.bindings[new_entity].relation,
                        entity_aliases[new_entity],
                    ),
                    on=_and_all(comparisons),
                    kind="INNER",
                )
            )
            joined.add(new_entity)
        inner.set("joins", inner_joins)

        def physical_predicate(item: BoundPredicate) -> exp.Expression:
            left = physical_field(item.logical_ref)
            entity, field_name = item.logical_ref.rsplit(".", 1)
            logical_type = self._domain.spec.entities[entity].fields[field_name].type
            if item.operator == "is_null":
                return exp.Is(this=left, expression=exp.Null())
            if item.operator == "is_not_null":
                return exp.Not(this=exp.Is(this=left, expression=exp.Null()))
            if isinstance(item.value, tuple):
                contained = exp.In(
                    this=left,
                    expressions=[
                        parameter(value, item.purpose, logical_type)
                        for value in item.value
                    ],
                )
                return exp.Not(this=contained) if item.operator == "not_in" else contained
            if item.value is None:
                raise BindingError(
                    BindingErrorCode.SQL_COMPILE_ERROR,
                    "aligned predicate has no value",
                )
            value: ParameterScalar = item.value
            if item.operator == "contains":
                value = f"%{value}%"
            right = parameter(value, item.purpose, logical_type)
            operation = {
                "eq": exp.EQ,
                "neq": exp.NEQ,
                "gt": exp.GT,
                "gte": exp.GTE,
                "lt": exp.LT,
                "lte": exp.LTE,
                "contains": exp.Like,
            }.get(item.operator)
            if operation is None:
                raise BindingError(
                    BindingErrorCode.SQL_COMPILE_ERROR,
                    "aligned predicate operator is unsupported",
                )
            return operation(this=left, expression=right)

        aggregate_refs = set(plan.metrics) | {
            item.id
            for item in plan.derived_calculations
            if self._calculation_is_aggregate(item.id, plan)
        }
        inner_where = [
            physical_predicate(item)
            for item in bound.predicates
            if item.logical_ref not in aggregate_refs
            and item.logical_ref not in calculations
        ]
        inner_where.extend(
            self._ownership_exists_expression(
                guard,
                entity_aliases,
                parameter,
            )
            for guard in bound.ownership_guards
        )
        if inner_where:
            inner.set("where", exp.Where(this=_and_all(inner_where)))

        aligned_alias = "aligned"

        def field(ref: str) -> exp.Column:
            try:
                alias = aligned_columns[ref]
            except KeyError as exc:
                raise BindingError(
                    BindingErrorCode.UNKNOWN_REFERENCE,
                    "aligned field was not projected by its protected grain",
                ) from exc
            return exp.Column(
                this=_quoted_identifier(alias),
                table=_quoted_identifier(aligned_alias),
            )

        def dimension(ref: str) -> exp.Expression:
            value: exp.Expression = field(ref)
            if (
                plan.time_grain is not None
                and plan.time_range is not None
                and plan.time_range.field == ref
            ):
                value = exp.func(
                    "DATE_TRUNC",
                    exp.Cast(
                        this=algorithm_parameter(plan.time_grain.value, "string"),
                        to=exp.DataType.build("TEXT", dialect="postgres"),
                    ),
                    value,
                )
            return value

        def metric(ref: str) -> exp.Expression:
            definition = self._domain.spec.metrics[ref]
            inputs = [field(item) for item in definition.inputs]
            if definition.aggregation == "sum":
                states = [exp.Sum(this=item) for item in inputs]
                return reduce(
                    lambda left, right: exp.Add(this=left, expression=right),
                    states,
                )
            if definition.aggregation == "average":
                return exp.Avg(this=inputs[0])
            if definition.aggregation == "count":
                return exp.Count(this=inputs[0])
            if definition.aggregation == "count_distinct":
                return exp.Count(this=exp.Distinct(expressions=[inputs[0]]))
            function = {"min": exp.Min, "max": exp.Max}.get(definition.aggregation)
            if function is None:
                raise BindingError(
                    BindingErrorCode.SQL_COMPILE_ERROR,
                    "aligned metric aggregation is unsupported",
                )
            return function(this=inputs[0])

        def reference(ref: str) -> exp.Expression:
            if ref in self._domain.spec.metrics:
                return metric(ref)
            if ref in calculations:
                return calculation(ref)
            return field(ref)

        def calculation(ref: str) -> exp.Expression:
            item = calculations[ref]
            inputs = [reference(value) for value in item.inputs]
            if item.operation == CalculationOperation.SUM:
                return exp.Sum(this=inputs[0])
            if item.operation == CalculationOperation.AVERAGE:
                return exp.Avg(this=inputs[0])
            if item.operation == CalculationOperation.COUNT:
                return exp.Count(this=inputs[0])
            if item.operation == CalculationOperation.COUNT_DISTINCT:
                return exp.Count(this=exp.Distinct(expressions=[inputs[0]]))
            if item.operation == CalculationOperation.COMPOSITE_KEY:
                return exp.Tuple(expressions=inputs)
            if item.operation == CalculationOperation.DATE_DIFFERENCE:
                return exp.Div(
                    this=exp.func(
                        "DATE_PART",
                        exp.Cast(
                            this=algorithm_parameter("epoch", "string"),
                            to=exp.DataType.build("TEXT", dialect="postgres"),
                        ),
                        exp.Sub(this=inputs[0], expression=inputs[1]),
                    ),
                    expression=algorithm_parameter(86400, "decimal"),
                )
            if item.operation == CalculationOperation.ADD:
                return reduce(
                    lambda left, right: exp.Add(this=left, expression=right),
                    inputs,
                )
            if item.operation == CalculationOperation.SUBTRACT:
                return exp.Sub(this=inputs[0], expression=inputs[1])
            if item.operation == CalculationOperation.MULTIPLY:
                return reduce(
                    lambda left, right: exp.Mul(this=left, expression=right),
                    inputs,
                )
            raise BindingError(
                BindingErrorCode.SQL_COMPILE_ERROR,
                "aligned calculation operation is unsupported",
            )

        output_expressions: list[exp.Expression] = []
        for selection in bound.selected_columns:
            if selection.kind == "dimension":
                value = dimension(selection.logical_ref)
            elif selection.kind == "field":
                value = field(selection.logical_ref)
            elif selection.kind == "metric":
                value = metric(selection.logical_ref)
            else:
                value = calculation(selection.logical_ref)
            output_expressions.append(
                exp.Alias(this=value, alias=_quoted_identifier(selection.alias))
            )
        query = exp.Select(expressions=output_expressions)
        query.set(
            "from_",
            exp.From(
                this=exp.Subquery(
                    this=inner,
                    alias=exp.TableAlias(this=_quoted_identifier(aligned_alias)),
                )
            ),
        )
        if bound.grouping and aggregate_refs:
            query.set(
                "group",
                exp.Group(
                    expressions=[
                        dimension(item.logical_ref) for item in bound.grouping
                    ]
                ),
            )

        having_items: list[exp.Expression] = []
        for predicate_item in plan.having:
            left = reference(predicate_item.ref)
            if predicate_item.value is None:
                value = (
                    exp.Is(this=left, expression=exp.Null())
                    if predicate_item.operator.value == "is_null"
                    else exp.Not(this=exp.Is(this=left, expression=exp.Null()))
                )
            elif isinstance(predicate_item.value, tuple):
                value = exp.In(
                    this=left,
                    expressions=[
                        parameter(item, "filter") for item in predicate_item.value
                    ],
                )
            else:
                right = parameter(predicate_item.value, "filter")
                operation = {
                    "eq": exp.EQ,
                    "neq": exp.NEQ,
                    "gt": exp.GT,
                    "gte": exp.GTE,
                    "lt": exp.LT,
                    "lte": exp.LTE,
                }[predicate_item.operator.value]
                value = operation(this=left, expression=right)
            having_items.append(value)
        if having_items:
            query.set("having", exp.Having(this=_and_all(having_items)))
        if bound.ordering:
            query.set(
                "order",
                exp.Order(
                    expressions=[
                        exp.Ordered(
                            this=(
                                exp.Column(
                                    this=_quoted_identifier(
                                        selection_aliases[item.logical_ref]
                                    )
                                )
                                if item.logical_ref in selection_aliases
                                else reference(item.logical_ref)
                            ),
                            desc=item.direction == "desc",
                        )
                        for item in bound.ordering
                    ]
                ),
            )
        query.set(
            "limit",
            exp.Limit(expression=parameter(bound.limit, "limit", "integer")),
        )
        sql = query.sql(dialect=self._dialect, pretty=False)
        return PreparedQuery(
            dialect=self._dialect,
            logical_plan=bound.logical_plan,
            logical_plan_hash=bound.logical_plan_hash,
            sql_ast_hash=hashlib.sha256(sql.encode("utf-8")).hexdigest(),
            logical_sql=sql,
            executable_sql=sql,
            parameters=tuple(parameters),
            allowed_relations=bound.physical_relations,
            policy_decision_id=bound.required_access.policy_decision_id,
            estimated_cost=0.0,
            max_rows=bound.limit,
            bundle_digest=self._bundle.digest,
            schema_fingerprint=self._bundle.schema_fingerprint,
        )

    def _apply_cast(
        self,
        expression: exp.Expression,
        binding: PhysicalFieldBinding,
    ) -> exp.Expression:
        if binding.cast is None:
            return expression
        if self._dialect == "sqlite" and binding.cast in {"date", "datetime"}:
            return exp.Anonymous(
                this="DATE" if binding.cast == "date" else "DATETIME",
                expressions=[expression],
            )
        sql_type = {
            "string": "TEXT",
            "integer": "BIGINT",
            "decimal": "DECIMAL",
            "boolean": "BOOLEAN",
            "date": "DATE",
            "datetime": "TIMESTAMP",
        }[binding.cast]
        return exp.Cast(
            this=expression,
            to=exp.DataType.build(sql_type, dialect="postgres"),
        )


def bind_logical_plan(
    logical_plan: LogicalQueryPlan,
    principal: PrincipalContext,
    domain_pack: DomainPack,
    enterprise_binding: EnterpriseDataBinding,
    bundle: ResolvedRuntimeBundle,
) -> BoundQueryPlan:
    return BindingCompiler(domain_pack, enterprise_binding, bundle).bind(
        logical_plan,
        principal,
    )


def compile_bound_query(
    bound_plan: BoundQueryPlan,
    principal: PrincipalContext,
    domain_pack: DomainPack,
    enterprise_binding: EnterpriseDataBinding,
    bundle: ResolvedRuntimeBundle,
    *,
    dialect: SqlDialect = "postgres",
) -> PreparedQuery:
    return BindingCompiler(
        domain_pack,
        enterprise_binding,
        bundle,
        dialect=dialect,
    ).compile(
        bound_plan,
        principal,
    )
