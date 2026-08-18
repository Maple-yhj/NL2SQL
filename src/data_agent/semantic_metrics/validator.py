"""Deterministic static validation for governed metric definitions."""

from __future__ import annotations

from collections.abc import Iterable

from data_agent.datasources.models import (
    SemanticBindingRecord,
    SemanticGraphBindingRecord,
)
from data_agent.relationships.grain import FanoutGuard, GraphFanoutError
from data_agent.relationships.router import (
    GraphRouteError,
    GraphRouteRequest,
    GraphRouteResolver,
)
from data_agent.tools.schemas import CatalogSnapshot

from .ast import (
    MetricAggregateFormula,
    MetricBinaryExpression,
    MetricFieldExpression,
    MetricFormulaBinary,
    MetricFormulaExpression,
    MetricFunctionExpression,
    MetricUnaryExpression,
    MetricValueExpression,
)
from .digest import semantic_digest
from .models import (
    MetricNullPolicy,
    MetricValidationIssue,
    MetricValidationSeverity,
    SemanticMetricDefinitionV2,
)


_NUMERIC_MARKERS = (
    "int",
    "real",
    "float",
    "double",
    "decimal",
    "numeric",
    "number",
    "money",
)
_TIME_MARKERS = ("date", "time", "timestamp", "datetime")
_VALIDATOR_VERSION = "semantic-metric-static-validator-v1"


class SemanticMetricStaticValidator:
    """Validate schema grounding and relationship safety without executing SQL."""

    @property
    def digest(self) -> str:
        return semantic_digest(
            {
                "version": _VALIDATOR_VERSION,
                "numeric_markers": _NUMERIC_MARKERS,
                "time_markers": _TIME_MARKERS,
            }
        )

    def validate(
        self,
        definition: SemanticMetricDefinitionV2,
        *,
        binding: SemanticBindingRecord | SemanticGraphBindingRecord,
        catalog: CatalogSnapshot,
    ) -> tuple[MetricValidationIssue, ...]:
        issues: list[MetricValidationIssue] = []
        if binding.status != "active":
            issues.append(
                self._error(
                    "METRIC_BINDING_INACTIVE",
                    "metric validation requires the active semantic binding",
                )
            )
        if isinstance(binding, SemanticGraphBindingRecord):
            if binding.schema_fingerprint != catalog.schema_fingerprint:
                issues.append(
                    self._error(
                        "METRIC_SCHEMA_STALE",
                        "binding and catalog schema fingerprints do not match",
                    )
                )

        mappings = {item.logical_ref: item for item in binding.mappings}
        unknown = tuple(ref for ref in definition.all_field_refs if ref not in mappings)
        if unknown:
            issues.append(
                self._error(
                    "METRIC_UNKNOWN_FIELD",
                    "metric references logical fields that are not in the active binding",
                    unknown,
                )
            )
            return tuple(issues)

        column_types, nullable = self._physical_metadata(binding, catalog)
        missing_columns = tuple(
            ref for ref in definition.all_field_refs if ref not in column_types
        )
        if missing_columns:
            issues.append(
                self._error(
                    "METRIC_PHYSICAL_FIELD_MISSING",
                    "metric fields are absent from the pinned catalog snapshot",
                    missing_columns,
                )
            )

        numeric_refs = self._numeric_field_refs(definition.formula)
        non_numeric = tuple(
            ref
            for ref in numeric_refs
            if ref in column_types and not self._is_numeric(column_types[ref])
        )
        if non_numeric:
            issues.append(
                self._error(
                    "METRIC_NON_NUMERIC_OPERAND",
                    "metric arithmetic requires numeric physical fields",
                    non_numeric,
                )
            )

        time_refs = tuple(
            dict.fromkeys(
                ref
                for ref in (definition.default_time_ref, *definition.allowed_time_refs)
                if ref is not None
            )
        )
        invalid_time_role = tuple(
            ref for ref in time_refs if mappings[ref].semantic_role != "time"
        )
        if invalid_time_role:
            issues.append(
                self._error(
                    "METRIC_INVALID_TIME_ROLE",
                    "metric time references must map to fields with semantic_role=time",
                    invalid_time_role,
                )
            )
        invalid_time_type = tuple(
            ref
            for ref in time_refs
            if ref in column_types and not self._is_time(column_types[ref])
        )
        if invalid_time_type:
            issues.append(
                self._error(
                    "METRIC_INVALID_TIME_TYPE",
                    "metric time references must map to date or timestamp columns",
                    invalid_time_type,
                )
            )

        invalid_entity_keys = tuple(
            ref
            for ref in definition.entity_key_refs
            if mappings[ref].semantic_role != "identifier"
        )
        if invalid_entity_keys:
            issues.append(
                self._error(
                    "METRIC_INVALID_ENTITY_KEY",
                    "metric entity keys must use identifier fields",
                    invalid_entity_keys,
                )
            )
        if definition.scope.status_ref is not None:
            status_mapping = mappings[definition.scope.status_ref]
            if status_mapping.semantic_role not in {"status", "dimension"}:
                issues.append(
                    self._error(
                        "METRIC_INVALID_STATUS_ROLE",
                        "scope status_ref must use a status or dimension field",
                        (definition.scope.status_ref,),
                    )
                )

        if definition.null_policy == MetricNullPolicy.ERROR:
            nullable_refs = tuple(
                ref for ref in numeric_refs if nullable.get(ref, True)
            )
            issues.append(
                self._error(
                    "METRIC_NULL_ERROR_UNSUPPORTED",
                    "null_policy=error is not executable until a non-null runtime "
                    "assertion contract is available",
                    nullable_refs or numeric_refs,
                )
            )

        issues.extend(self._validate_route(definition, binding))
        return tuple(issues)

    @staticmethod
    def _physical_metadata(
        binding: SemanticBindingRecord | SemanticGraphBindingRecord,
        catalog: CatalogSnapshot,
    ) -> tuple[dict[str, str], dict[str, bool]]:
        types: dict[str, str] = {}
        nullable: dict[str, bool] = {}
        if isinstance(binding, SemanticBindingRecord):
            physical = {
                (relation.relation, column.name): column
                for relation in catalog.relations
                for column in relation.columns
            }
            for mapping in binding.mappings:
                column = physical.get(
                    (mapping.physical_relation, mapping.physical_column)
                )
                if column is not None:
                    types[mapping.logical_ref] = column.data_type
                    nullable[mapping.logical_ref] = column.nullable
            return types, nullable

        columns = {
            column.column_id: column
            for relation in catalog.relations
            for column in relation.columns
        }
        for mapping in binding.mappings:
            column = columns.get(mapping.column_id)
            if column is not None:
                types[mapping.logical_ref] = column.data_type
                nullable[mapping.logical_ref] = column.nullable
        return types, nullable

    def _validate_route(
        self,
        definition: SemanticMetricDefinitionV2,
        binding: SemanticBindingRecord | SemanticGraphBindingRecord,
    ) -> tuple[MetricValidationIssue, ...]:
        if not isinstance(binding, SemanticGraphBindingRecord):
            mappings = {item.logical_ref: item for item in binding.mappings}
            relations = {
                mappings[ref].physical_relation for ref in definition.ast_field_refs
            }
            if len(relations) > 1:
                return (
                    MetricValidationIssue(
                        severity=MetricValidationSeverity.ERROR,
                        code="METRIC_GRAIN_UNVERIFIED",
                        message=(
                            "multi-relation metrics require a v2 relationship graph "
                            "with cardinality and grain metadata"
                        ),
                        field_refs=definition.ast_field_refs,
                    ),
                )
            return ()

        mappings = {item.logical_ref: item for item in binding.mappings}
        required_nodes = tuple(
            dict.fromkeys(mappings[ref].node_id for ref in definition.ast_field_refs)
        )
        if not required_nodes:
            return ()
        try:
            route = GraphRouteResolver().resolve(
                binding.graph,
                GraphRouteRequest(
                    required_node_ids=required_nodes,
                    required_logical_refs=definition.ast_field_refs,
                ),
            )
        except GraphRouteError as exc:
            return (
                self._error(
                    exc.code,
                    f"metric fields do not have a safe relationship route: {exc}",
                    definition.ast_field_refs,
                ),
            )
        measure_refs = self._measure_field_refs(definition.formula)
        try:
            FanoutGuard().require_safe(
                graph=binding.graph,
                route=route,
                measure_node_ids=tuple(
                    dict.fromkeys(mappings[ref].node_id for ref in measure_refs)
                ),
                analysis_type="aggregate",
            )
        except GraphFanoutError as exc:
            return (
                self._error(
                    exc.code,
                    f"metric aggregation is unsafe across the selected route: {exc}",
                    measure_refs,
                ),
            )
        return ()

    @classmethod
    def _numeric_field_refs(
        cls,
        formula: MetricFormulaExpression,
    ) -> tuple[str, ...]:
        refs: list[str] = []

        def visit_formula(item: MetricFormulaExpression) -> None:
            if isinstance(item, MetricAggregateFormula):
                if item.operand is not None and (
                    item.operation in {"sum", "avg", "median"}
                    or cls._contains_arithmetic(item.operand)
                ):
                    refs.extend(cls._value_field_refs(item.operand))
            elif isinstance(item, MetricFormulaBinary):
                visit_formula(item.left)
                visit_formula(item.right)

        visit_formula(formula)
        return tuple(dict.fromkeys(refs))

    @classmethod
    def _measure_field_refs(
        cls,
        formula: MetricFormulaExpression,
    ) -> tuple[str, ...]:
        refs: list[str] = []

        def visit(item: MetricFormulaExpression) -> None:
            if isinstance(item, MetricAggregateFormula):
                if item.operand is not None:
                    refs.extend(cls._value_field_refs(item.operand))
            elif isinstance(item, MetricFormulaBinary):
                visit(item.left)
                visit(item.right)

        visit(formula)
        return tuple(dict.fromkeys(refs))

    @classmethod
    def _value_field_refs(cls, value: MetricValueExpression) -> tuple[str, ...]:
        if isinstance(value, MetricFieldExpression):
            return (value.ref,)
        if isinstance(value, MetricUnaryExpression):
            return cls._value_field_refs(value.operand)
        if isinstance(value, MetricBinaryExpression):
            return tuple(
                dict.fromkeys(
                    (
                        *cls._value_field_refs(value.left),
                        *cls._value_field_refs(value.right),
                    )
                )
            )
        if isinstance(value, MetricFunctionExpression):
            return tuple(
                dict.fromkeys(
                    ref
                    for argument in value.arguments
                    for ref in cls._value_field_refs(argument)
                )
            )
        return ()

    @classmethod
    def _contains_arithmetic(cls, value: MetricValueExpression) -> bool:
        if isinstance(value, (MetricBinaryExpression, MetricUnaryExpression)):
            return True
        if isinstance(value, MetricFunctionExpression):
            return value.operation in {"cast_decimal", "abs"} or any(
                cls._contains_arithmetic(argument) for argument in value.arguments
            )
        return False

    @staticmethod
    def _is_numeric(data_type: str) -> bool:
        lowered = data_type.casefold()
        return any(marker in lowered for marker in _NUMERIC_MARKERS)

    @staticmethod
    def _is_time(data_type: str) -> bool:
        lowered = data_type.casefold()
        return any(marker in lowered for marker in _TIME_MARKERS)

    @staticmethod
    def _error(
        code: str,
        message: str,
        field_refs: Iterable[str] = (),
    ) -> MetricValidationIssue:
        return MetricValidationIssue(
            severity=MetricValidationSeverity.ERROR,
            code=code,
            message=message,
            field_refs=tuple(dict.fromkeys(field_refs)),
        )


__all__ = ["SemanticMetricStaticValidator"]
