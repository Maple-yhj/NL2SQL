"""Deterministic, DomainPack-only validation for logical query plans."""

from __future__ import annotations

import re
import math
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ValidationError

from data_agent.runtime.packs import CanonicalRelationship, DomainPack

from .models import (
    AnalysisType,
    CalculationOperation,
    GrainAlignment,
    LogicalQueryPlan,
    ResultShape,
    SkillModel,
)


class PlanValidationCode(StrEnum):
    RAW_SQL_FORBIDDEN = "RAW_SQL_FORBIDDEN"
    PHYSICAL_IDENTIFIER_FORBIDDEN = "PHYSICAL_IDENTIFIER_FORBIDDEN"
    INVALID_PLAN_CONTRACT = "INVALID_PLAN_CONTRACT"
    NON_FINITE_NUMBER = "NON_FINITE_NUMBER"
    DOMAIN_MISMATCH = "DOMAIN_MISMATCH"
    EMPTY_RESULT = "EMPTY_RESULT"
    DUPLICATE_REFERENCE = "DUPLICATE_REFERENCE"
    UNKNOWN_METRIC = "UNKNOWN_METRIC"
    UNKNOWN_ENTITY = "UNKNOWN_ENTITY"
    UNKNOWN_FIELD = "UNKNOWN_FIELD"
    UNKNOWN_RELATIONSHIP = "UNKNOWN_RELATIONSHIP"
    UNKNOWN_REFERENCE = "UNKNOWN_REFERENCE"
    ENTITY_DECLARATION_REQUIRED = "ENTITY_DECLARATION_REQUIRED"
    UNREACHABLE_ENTITY = "UNREACHABLE_ENTITY"
    INVALID_CALCULATION = "INVALID_CALCULATION"
    PREDICATE_TYPE_MISMATCH = "PREDICATE_TYPE_MISMATCH"
    INVALID_HAVING = "INVALID_HAVING"
    WINDOW_REQUIRED = "WINDOW_REQUIRED"
    INVALID_WINDOW = "INVALID_WINDOW"
    FANOUT_ALIGNMENT_REQUIRED = "FANOUT_ALIGNMENT_REQUIRED"
    UNSAFE_GRAIN_ALIGNMENT = "UNSAFE_GRAIN_ALIGNMENT"
    METRIC_GRAIN_INCOMPATIBLE = "METRIC_GRAIN_INCOMPATIBLE"
    INVALID_TIME_FIELD = "INVALID_TIME_FIELD"
    INCOMPLETE_TIME_RANGE = "INCOMPLETE_TIME_RANGE"
    METRIC_TIME_FIELD_MISMATCH = "METRIC_TIME_FIELD_MISMATCH"
    INVALID_ORDERING = "INVALID_ORDERING"
    ORDERING_REQUIRED = "ORDERING_REQUIRED"
    UNBOUNDED_DETAIL = "UNBOUNDED_DETAIL"
    EXPECTED_GRAIN_MISMATCH = "EXPECTED_GRAIN_MISMATCH"
    RESULT_SHAPE_MISMATCH = "RESULT_SHAPE_MISMATCH"
    ANALYSIS_SEMANTICS_MISMATCH = "ANALYSIS_SEMANTICS_MISMATCH"
    CONTEXT_MISMATCH = "CONTEXT_MISMATCH"


class PlanValidationIssue(SkillModel):
    code: PlanValidationCode
    path: str
    message: str


class PlanValidationResult(SkillModel):
    valid: bool
    plan_hash: str | None = None
    issues: tuple[PlanValidationIssue, ...] = ()


class PlanValidationError(ValueError):
    """Raised when a caller requires a valid plan but validation fails."""

    def __init__(self, result: PlanValidationResult) -> None:
        self.result = result
        self.codes = tuple(issue.code for issue in result.issues)
        joined = ",".join(code.value for code in self.codes)
        super().__init__(f"logical plan validation failed: {joined}")


_RAW_QUERY_KEYS = {
    "sql",
    "rawsql",
    "querytext",
    "statement",
    "expression",
}
_PHYSICAL_KEYS = {
    "database",
    "schema",
    "table",
    "relation",
    "column",
    "connector",
    "connection",
    "connectionref",
    "credential",
    "credentials",
    "dsn",
}
_RAW_QUERY_PATTERN = re.compile(
    r"(?:^\s*(?:select|with|insert|update|delete|drop|alter|create)\b|"
    r"\bselect\b.{0,512}\bfrom\b|\bgroup\s+by\b|\border\s+by\b|"
    r"\bunion\s+(?:all\s+)?select\b)",
    re.IGNORECASE,
)
_PHYSICAL_VALUE_PATTERN = re.compile(
    r"(?:\b(?:jdbc|odbc|postgres|mysql|sqlite)\b|(?:secret|postgresql?)://)",
    re.IGNORECASE,
)
_QUALIFIED_IDENTIFIER_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_])(?:[a-z_][a-z0-9_]*\.)+[a-z_][a-z0-9_]*(?![A-Za-z0-9_])",
    re.IGNORECASE,
)
_PHYSICAL_DATASET_PATTERN = re.compile(r"\bolist(?:_[a-z0-9]+)*\b", re.IGNORECASE)
_PHYSICAL_FIELD_STYLE_PATTERN = re.compile(
    r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)*_"
    r"(?:id|timestamp|date|value|qty|type|status|score|state|city|lat|lng|column|table)\b",
    re.IGNORECASE,
)
_CREDENTIAL_PATTERN = re.compile(
    r"\b(?:password|passwd|api[_-]?key|access[_-]?token|credential|secret)\s*[:=]",
    re.IGNORECASE,
)
_REFERENCE_VALUE_KEYS = {
    "ref",
    "field",
    "id",
    "calculation",
    "axisref",
    "measure",
    "metrics",
    "entities",
    "relationships",
    "dimensions",
    "fields",
    "expectedgrain",
    "inputs",
    "partitionby",
    "outputgrain",
    "relationshippath",
    "joingrain",
    "rowaxis",
    "columnaxis",
    "values",
    "sourceentity",
    "targetentity",
}
_AGGREGATE_OPERATIONS = {
    CalculationOperation.SUM,
    CalculationOperation.AVERAGE,
    CalculationOperation.COUNT,
    CalculationOperation.COUNT_DISTINCT,
}
_WINDOW_OPERATIONS = {
    CalculationOperation.GROWTH,
    CalculationOperation.LAG,
}


class _LogicalType(StrEnum):
    STRING = "string"
    INTEGER = "integer"
    DECIMAL = "decimal"
    BOOLEAN = "boolean"
    DATE = "date"
    DATETIME = "datetime"
    JSON = "json"
    DURATION = "duration"
    COMPOSITE = "composite"


_NUMERIC_TYPES = {_LogicalType.INTEGER, _LogicalType.DECIMAL}
_TEMPORAL_TYPES = {_LogicalType.DATE, _LogicalType.DATETIME}


def _normalized_key(value: object) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value).lower())


def _issue(
    code: PlanValidationCode,
    path: str,
    message: str,
) -> PlanValidationIssue:
    return PlanValidationIssue(code=code, path=path, message=message)


def _stable_issues(
    issues: Sequence[PlanValidationIssue],
) -> tuple[PlanValidationIssue, ...]:
    priority = {
        PlanValidationCode.RAW_SQL_FORBIDDEN: 0,
        PlanValidationCode.PHYSICAL_IDENTIFIER_FORBIDDEN: 1,
        PlanValidationCode.NON_FINITE_NUMBER: 2,
        PlanValidationCode.INVALID_PLAN_CONTRACT: 3,
    }
    unique = {
        (item.code, item.path, item.message): item
        for item in issues
    }
    return tuple(
        sorted(
            unique.values(),
            key=lambda item: (
                priority.get(item.code, 10),
                item.code.value,
                item.path,
                item.message,
            ),
        )
    )


def _unknown_field_issue(key: object, path: str) -> PlanValidationIssue:
    normalized = _normalized_key(key)
    if normalized in _RAW_QUERY_KEYS:
        return _issue(
            PlanValidationCode.RAW_SQL_FORBIDDEN,
            path,
            "raw query fields are forbidden",
        )
    if normalized in _PHYSICAL_KEYS:
        return _issue(
            PlanValidationCode.PHYSICAL_IDENTIFIER_FORBIDDEN,
            path,
            "physical configuration fields are forbidden",
        )
    return _issue(
        PlanValidationCode.INVALID_PLAN_CONTRACT,
        path,
        "undeclared model fields are forbidden",
    )


def _scan_model_integrity(
    value: object,
    path: str = "plan",
) -> list[PlanValidationIssue]:
    """Inspect actual model storage before schema serialization can hide extras."""

    issues: list[PlanValidationIssue] = []
    if isinstance(value, BaseModel):
        fields = type(value).model_fields
        stored = getattr(value, "__dict__", {})
        for key in sorted(set(stored) - set(fields)):
            nested_path = f"{path}.{key}"
            issues.append(_unknown_field_issue(key, nested_path))
            issues.extend(_scan_forbidden_payload(stored[key], nested_path))
        extra = getattr(value, "__pydantic_extra__", None)
        if isinstance(extra, Mapping):
            for key in sorted(extra, key=lambda item: str(item)):
                nested_path = f"{path}.{key}"
                issues.append(_unknown_field_issue(key, nested_path))
                issues.extend(_scan_forbidden_payload(extra[key], nested_path))
        for field_name in fields:
            if field_name in stored:
                issues.extend(
                    _scan_model_integrity(
                        stored[field_name],
                        f"{path}.{field_name}",
                    )
                )
        return issues
    if isinstance(value, Mapping):
        for key in sorted(value, key=lambda item: str(item)):
            issues.extend(_scan_model_integrity(value[key], f"{path}.{key}"))
        return issues
    if isinstance(value, (list, tuple)):
        for index, nested in enumerate(value):
            issues.extend(_scan_model_integrity(nested, f"{path}[{index}]"))
        return issues
    if isinstance(value, float) and not math.isfinite(value):
        issues.append(
            _issue(
                PlanValidationCode.NON_FINITE_NUMBER,
                path,
                "numeric values must be finite",
            )
        )
    return issues


def _contains_physical_identifier(
    value: str,
    allowed_logical_refs: frozenset[str],
) -> bool:
    if value == "context.seller_id":
        return False
    characters = list(value)
    for match in _QUALIFIED_IDENTIFIER_PATTERN.finditer(value):
        if match.group(0) not in allowed_logical_refs:
            return True
        for index in range(match.start(), match.end()):
            characters[index] = " "
    remaining = "".join(characters)
    return bool(
        _PHYSICAL_DATASET_PATTERN.search(remaining)
        or _PHYSICAL_FIELD_STYLE_PATTERN.search(remaining)
    )


def _scan_forbidden_payload(
    value: object,
    path: str = "plan",
    *,
    logical_reference: bool = False,
    allowed_logical_refs: frozenset[str] = frozenset(),
) -> list[PlanValidationIssue]:
    issues: list[PlanValidationIssue] = []
    if isinstance(value, Mapping):
        for key in sorted(value, key=lambda item: str(item)):
            nested = value[key]
            normalized = _normalized_key(key)
            nested_path = f"{path}.{key}"
            if normalized in _RAW_QUERY_KEYS:
                issues.append(
                    _issue(
                        PlanValidationCode.RAW_SQL_FORBIDDEN,
                        nested_path,
                        "raw query fields are forbidden",
                    )
                )
                continue
            if normalized in _PHYSICAL_KEYS:
                issues.append(
                    _issue(
                        PlanValidationCode.PHYSICAL_IDENTIFIER_FORBIDDEN,
                        nested_path,
                        "physical configuration fields are forbidden",
                    )
                )
                continue
            issues.extend(
                _scan_forbidden_payload(
                    nested,
                    nested_path,
                    logical_reference=normalized in _REFERENCE_VALUE_KEYS,
                    allowed_logical_refs=allowed_logical_refs,
                )
            )
        return issues
    if isinstance(value, (list, tuple)):
        for index, nested in enumerate(value):
            issues.extend(
                _scan_forbidden_payload(
                    nested,
                    f"{path}[{index}]",
                    logical_reference=logical_reference,
                    allowed_logical_refs=allowed_logical_refs,
                )
            )
        return issues
    if isinstance(value, float) and not math.isfinite(value):
        issues.append(
            _issue(
                PlanValidationCode.NON_FINITE_NUMBER,
                path,
                "numeric values must be finite",
            )
        )
        return issues
    if isinstance(value, str):
        if _RAW_QUERY_PATTERN.search(value):
            issues.append(
                _issue(
                    PlanValidationCode.RAW_SQL_FORBIDDEN,
                    path,
                    "raw query text is forbidden",
                )
            )
        elif (
            _PHYSICAL_VALUE_PATTERN.search(value)
            or _CREDENTIAL_PATTERN.search(value)
            or (
                not logical_reference
                and _contains_physical_identifier(value, allowed_logical_refs)
            )
        ):
            issues.append(
                _issue(
                    PlanValidationCode.PHYSICAL_IDENTIFIER_FORBIDDEN,
                    path,
                    "connector or credential text is forbidden",
                )
            )
    return issues


@dataclass(frozen=True)
class _PathStep:
    relationship: CanonicalRelationship
    source: str
    target: str
    expands: bool

    @property
    def many_to_many(self) -> bool:
        return self.relationship.cardinality == "many_to_many"


@dataclass(frozen=True)
class _MeasureSource:
    entity: str
    duplicate_safe: bool


def _step_expands(relationship: CanonicalRelationship, forward: bool) -> bool:
    if relationship.cardinality == "many_to_many":
        return True
    if relationship.cardinality == "one_to_one":
        return False
    if relationship.cardinality == "many_to_one":
        return not forward
    return forward


def _adjacency(
    relationships: Sequence[CanonicalRelationship],
) -> dict[str, tuple[_PathStep, ...]]:
    mutable: dict[str, list[_PathStep]] = {}
    for relationship in relationships:
        mutable.setdefault(relationship.from_entity, []).append(
            _PathStep(
                relationship=relationship,
                source=relationship.from_entity,
                target=relationship.to_entity,
                expands=_step_expands(relationship, True),
            )
        )
        mutable.setdefault(relationship.to_entity, []).append(
            _PathStep(
                relationship=relationship,
                source=relationship.to_entity,
                target=relationship.from_entity,
                expands=_step_expands(relationship, False),
            )
        )
    return {
        entity: tuple(
            sorted(
                steps,
                key=lambda step: (step.relationship.name, step.target),
            )
        )
        for entity, steps in mutable.items()
    }


def _find_path(
    source: str,
    target: str,
    relationships: Sequence[CanonicalRelationship],
) -> tuple[_PathStep, ...] | None:
    if source == target:
        return ()
    graph = _adjacency(relationships)
    queue: deque[tuple[str, tuple[_PathStep, ...]]] = deque([(source, ())])
    visited = {source}
    while queue:
        entity, path = queue.popleft()
        for step in graph.get(entity, ()):
            if step.target in visited:
                continue
            next_path = (*path, step)
            if step.target == target:
                return next_path
            visited.add(step.target)
            queue.append((step.target, next_path))
    return None


def relationship_ids_for_entities(
    entities: Sequence[str],
    domain_pack: DomainPack,
) -> tuple[str, ...]:
    """Return a deterministic minimal relationship union for declared entities."""

    if len(entities) < 2:
        return ()
    relationships = domain_pack.spec.relationships
    anchor = entities[0]
    result: list[str] = []
    for target in entities[1:]:
        path = _find_path(anchor, target, relationships)
        if path is None:
            continue
        for step in path:
            if step.relationship.name not in result:
                result.append(step.relationship.name)
    return tuple(result)


def _effective_dimensions(plan: LogicalQueryPlan) -> tuple[str, ...]:
    constant_refs = {
        predicate.ref
        for predicate in plan.filters
        if predicate.operator == "eq"
        or (
            predicate.operator == "in"
            and isinstance(predicate.value, tuple)
            and len(predicate.value) == 1
        )
    }
    if plan.context.tenant_scope == "seller":
        constant_refs.add("commerce.Seller.seller_id")
        constant_refs.add("commerce.OrderItem.seller_id")
    return tuple(
        dimension
        for dimension in plan.dimensions
        if dimension not in constant_refs
    )


def expected_result_shape(plan: LogicalQueryPlan) -> ResultShape:
    """Derive the only accepted result family from plan semantics."""

    analysis_type = plan.analysis_type
    if plan.fields or analysis_type == AnalysisType.DETAIL:
        return ResultShape.DETAIL
    if analysis_type == AnalysisType.TREND:
        if plan.ranking is not None and plan.ranking.mode == "top_n":
            return ResultShape.RANKING
        if plan.series_axis is not None and plan.series_axis.kind == "time":
            return ResultShape.TIME_SERIES
        return ResultShape.TABLE
    if analysis_type == AnalysisType.RANKING:
        return ResultShape.RANKING
    if analysis_type == AnalysisType.CROSS_TAB:
        return ResultShape.CROSS_TAB
    if analysis_type == AnalysisType.DISTRIBUTION:
        return ResultShape.DISTRIBUTION
    if analysis_type == AnalysisType.COMPARISON:
        return ResultShape.TABLE
    dimensions = plan.expected_grain or _effective_dimensions(plan)
    if analysis_type in {AnalysisType.FOLLOW_UP, AnalysisType.TENANT_SCOPED}:
        if plan.ranking is not None:
            return ResultShape.RANKING
        if plan.series_axis is not None and plan.series_axis.kind == "time":
            return ResultShape.TIME_SERIES
        ordered_outputs = set(plan.metrics) | {
            calculation.id for calculation in plan.derived_calculations
        }
        if dimensions and plan.ordering and plan.ordering[0].ref in ordered_outputs:
            return ResultShape.RANKING
    if analysis_type == AnalysisType.DERIVED and plan.fields:
        return ResultShape.DETAIL
    return ResultShape.TABLE if dimensions else ResultShape.SCALAR


class CommercePlanValidator:
    """Validate one Commerce logical plan using only its DomainPack snapshot."""

    def validate(
        self,
        plan: LogicalQueryPlan | Mapping[str, Any],
        domain_pack: DomainPack,
    ) -> PlanValidationResult:
        integrity = (
            _stable_issues(_scan_model_integrity(plan))
            if isinstance(plan, BaseModel)
            else ()
        )
        if integrity:
            return PlanValidationResult(valid=False, issues=integrity)

        raw = (
            plan.model_dump(mode="json", by_alias=True, warnings=False)
            if isinstance(plan, LogicalQueryPlan)
            else plan
        )
        declared_fields = {
            f"{entity_id}.{field_name}"
            for entity_id, entity in domain_pack.spec.entities.items()
            for field_name in entity.fields
        }
        allowed_logical_refs = frozenset(
            set(domain_pack.spec.entities)
            | set(domain_pack.spec.metrics)
            | {item.name for item in domain_pack.spec.relationships}
            | declared_fields
        )
        forbidden = _stable_issues(
            _scan_forbidden_payload(
                raw,
                allowed_logical_refs=allowed_logical_refs,
            )
        )
        if forbidden:
            return PlanValidationResult(valid=False, issues=forbidden)

        try:
            plan = LogicalQueryPlan.model_validate(raw)
        except ValidationError:
            issue = _issue(
                PlanValidationCode.INVALID_PLAN_CONTRACT,
                "plan",
                "plan does not satisfy LogicalQueryPlan",
            )
            return PlanValidationResult(valid=False, issues=(issue,))

        issues = self._validate_semantics(plan, domain_pack)
        stable = _stable_issues(issues)
        return PlanValidationResult(
            valid=not stable,
            plan_hash=plan.stable_hash(),
            issues=stable,
        )

    def require_valid(
        self,
        plan: LogicalQueryPlan | Mapping[str, Any],
        domain_pack: DomainPack,
    ) -> PlanValidationResult:
        result = self.validate(plan, domain_pack)
        if not result.valid:
            raise PlanValidationError(result)
        return result

    def suggest_grain_alignment(
        self,
        plan: LogicalQueryPlan,
        domain_pack: DomainPack,
    ) -> tuple[GrainAlignment, ...]:
        """Create explicit metadata for every expanding measure path.

        This helper is used only while constructing trusted built-in fixtures;
        validation never inserts or assumes an alignment on a submitted plan.
        """

        relationships_by_name = {
            relationship.name: relationship
            for relationship in domain_pack.spec.relationships
        }
        declared = tuple(
            relationships_by_name[name]
            for name in plan.relationships
            if name in relationships_by_name
        )
        alignments: list[GrainAlignment] = []
        for source, target, path, duplicate_safe in self._dangerous_paths(
            plan,
            domain_pack,
            declared,
        ):
            has_many_to_many = any(step.many_to_many for step in path)
            if duplicate_safe and not has_many_to_many:
                continue
            alignments.append(
                GrainAlignment(
                    source_entity=source,
                    target_entity=target,
                    strategy="distinct" if duplicate_safe else "pre_aggregate",
                    relationship_path=tuple(
                        step.relationship.name for step in path
                    ),
                    join_grain=self._join_grain(path),
                )
            )
        return tuple(
            sorted(
                {
                    (
                        item.source_entity,
                        item.target_entity,
                        item.relationship_path,
                    ): item
                    for item in alignments
                }.values(),
                key=lambda item: (
                    item.source_entity,
                    item.target_entity,
                    item.relationship_path,
                ),
            )
        )

    def _validate_semantics(
        self,
        plan: LogicalQueryPlan,
        domain_pack: DomainPack,
    ) -> list[PlanValidationIssue]:
        issues: list[PlanValidationIssue] = []
        spec = domain_pack.spec
        entities = spec.entities
        metrics = spec.metrics
        relationships_by_name = {
            relationship.name: relationship
            for relationship in spec.relationships
        }
        fields = {
            f"{entity_id}.{field_name}": (entity_id, field)
            for entity_id, entity in entities.items()
            for field_name, field in entity.fields.items()
        }

        if domain_pack.metadata.name != "commerce":
            issues.append(
                _issue(
                    PlanValidationCode.DOMAIN_MISMATCH,
                    "domain_pack.metadata.name",
                    "commerce.analytics requires the commerce DomainPack",
                )
            )
        if not plan.metrics and not plan.fields and not plan.derived_calculations:
            issues.append(
                _issue(
                    PlanValidationCode.EMPTY_RESULT,
                    "plan",
                    "plan must request a metric, field, or calculation",
                )
            )

        self._check_duplicates(plan, issues)

        for index, metric in enumerate(plan.metrics):
            if metric not in metrics:
                issues.append(
                    _issue(
                        PlanValidationCode.UNKNOWN_METRIC,
                        f"metrics[{index}]",
                        f"metric {metric!r} is not declared",
                    )
                )
        for index, entity in enumerate(plan.entities):
            if entity not in entities:
                issues.append(
                    _issue(
                        PlanValidationCode.UNKNOWN_ENTITY,
                        f"entities[{index}]",
                        f"entity {entity!r} is not declared",
                    )
                )
        known_relationships: list[CanonicalRelationship] = []
        for index, relationship_name in enumerate(plan.relationships):
            relationship = relationships_by_name.get(relationship_name)
            if relationship is None:
                issues.append(
                    _issue(
                        PlanValidationCode.UNKNOWN_RELATIONSHIP,
                        f"relationships[{index}]",
                        f"relationship {relationship_name!r} is not declared",
                    )
                )
            else:
                known_relationships.append(relationship)

        field_locations = (
            ("dimensions", plan.dimensions),
            ("fields", plan.fields),
            ("expected_grain", plan.expected_grain),
        )
        for location, refs in field_locations:
            for index, ref in enumerate(refs):
                if ref not in fields:
                    issues.append(
                        _issue(
                            PlanValidationCode.UNKNOWN_FIELD,
                            f"{location}[{index}]",
                            f"field {ref!r} is not declared",
                        )
                    )

        field_types = {
            ref: _LogicalType(field.type)
            for ref, (_, field) in fields.items()
        }
        metric_types = self._metric_types(metrics, field_types, plan.metrics, issues)
        (
            calculation_ids,
            aggregate_calculations,
            reference_types,
        ) = self._validate_calculations(
            plan,
            fields,
            metrics,
            field_types,
            metric_types,
            set(relationships_by_name),
            domain_pack.metadata.name,
            issues,
        )
        allowed_refs = set(fields) | set(metrics) | calculation_ids
        for location, predicates in (("filters", plan.filters), ("having", plan.having)):
            for index, predicate in enumerate(predicates):
                if predicate.ref not in allowed_refs:
                    issues.append(
                        _issue(
                            PlanValidationCode.UNKNOWN_REFERENCE,
                            f"{location}[{index}].ref",
                            f"reference {predicate.ref!r} is not declared",
                        )
                    )
        for index, predicate in enumerate(plan.having):
            if (
                predicate.ref not in metrics
                and predicate.ref not in aggregate_calculations
            ):
                issues.append(
                    _issue(
                        PlanValidationCode.INVALID_HAVING,
                        f"having[{index}].ref",
                        "HAVING must reference a metric or aggregate calculation",
                    )
                )

        self._validate_predicate_types(plan, reference_types, issues)
        self._validate_windows(
            plan,
            fields,
            calculation_ids,
            set(metrics) | set(relationships_by_name) | calculation_ids,
            domain_pack.metadata.name,
            issues,
        )
        self._validate_analysis_semantics(
            plan,
            fields,
            reference_types,
            calculation_ids,
            aggregate_calculations,
            issues,
        )
        required_entities = self._required_entities(plan, fields, metrics)
        self._validate_entity_declarations(
            plan,
            required_entities,
            known_relationships,
            issues,
        )
        self._validate_time(plan, fields, metrics, issues)
        self._validate_order_limit_shape(
            plan,
            allowed_refs,
            entities,
            tuple(known_relationships),
            issues,
        )
        self._validate_fanout(
            plan,
            domain_pack,
            tuple(known_relationships),
            fields,
            relationships_by_name,
            issues,
        )
        return issues

    @staticmethod
    def _check_duplicates(
        plan: LogicalQueryPlan,
        issues: list[PlanValidationIssue],
    ) -> None:
        collections = {
            "metrics": plan.metrics,
            "entities": plan.entities,
            "relationships": plan.relationships,
            "dimensions": plan.dimensions,
            "fields": plan.fields,
            "expected_grain": plan.expected_grain,
        }
        for location, values in collections.items():
            if len(values) != len(set(values)):
                issues.append(
                    _issue(
                        PlanValidationCode.DUPLICATE_REFERENCE,
                        location,
                        f"{location} must not contain duplicate references",
                    )
                )

    @staticmethod
    def _metric_types(
        metrics: Mapping[str, object],
        field_types: Mapping[str, _LogicalType],
        requested_metrics: Sequence[str],
        issues: list[PlanValidationIssue],
    ) -> dict[str, _LogicalType]:
        result: dict[str, _LogicalType] = {}
        requested = set(requested_metrics)
        for metric_id, metric in metrics.items():
            input_types = [
                field_types[input_ref]
                for input_ref in metric.inputs
                if input_ref in field_types
            ]
            output_type: _LogicalType | None = None
            if metric.aggregation in {"count", "count_distinct"}:
                output_type = _LogicalType.INTEGER
            elif metric.aggregation == "average":
                if input_types and all(item in _NUMERIC_TYPES for item in input_types):
                    output_type = _LogicalType.DECIMAL
            elif metric.aggregation == "sum":
                if input_types and all(item in _NUMERIC_TYPES for item in input_types):
                    output_type = (
                        _LogicalType.DECIMAL
                        if _LogicalType.DECIMAL in input_types
                        else _LogicalType.INTEGER
                    )
            elif metric.aggregation in {"min", "max"}:
                if input_types and len(set(input_types)) == 1:
                    output_type = input_types[0]
            if output_type is None:
                if metric_id in requested:
                    issues.append(
                        _issue(
                            PlanValidationCode.INVALID_CALCULATION,
                            f"metrics.{metric_id}",
                            "metric aggregation inputs have incompatible canonical types",
                        )
                    )
                continue
            result[metric_id] = output_type
        return result

    def _validate_calculations(
        self,
        plan: LogicalQueryPlan,
        fields: Mapping[str, object],
        metrics: Mapping[str, object],
        field_types: Mapping[str, _LogicalType],
        metric_types: Mapping[str, _LogicalType],
        relationship_ids: set[str],
        domain_name: str,
        issues: list[PlanValidationIssue],
    ) -> tuple[set[str], set[str], dict[str, _LogicalType]]:
        available = set(fields) | set(metrics)
        available_types = {**field_types, **metric_types}
        ids: set[str] = set()
        aggregate: set[str] = set(metrics)
        reserved_aliases = set(metrics) | relationship_ids
        for index, calculation in enumerate(plan.derived_calculations):
            location = f"derived_calculations[{index}]"
            if not calculation.id.startswith(f"{domain_name}."):
                issues.append(
                    _issue(
                        PlanValidationCode.INVALID_CALCULATION,
                        f"{location}.id",
                        "calculation ID must use the DomainPack namespace",
                    )
                )
            if calculation.id in available or calculation.id in reserved_aliases:
                issues.append(
                    _issue(
                        PlanValidationCode.DUPLICATE_REFERENCE,
                        f"{location}.id",
                        "calculation ID must be unique in the logical namespace",
                    )
                )
            missing = set(calculation.inputs) - available
            if missing:
                issues.append(
                    _issue(
                        PlanValidationCode.INVALID_CALCULATION,
                        f"{location}.inputs",
                        "calculation inputs must reference prior logical outputs",
                    )
                )
            if set(calculation.partition_by) - set(fields):
                issues.append(
                    _issue(
                        PlanValidationCode.INVALID_CALCULATION,
                        f"{location}.partition_by",
                        "calculation partitions must be declared fields",
                    )
                )
            input_count = len(calculation.inputs)
            if (
                calculation.operation in _AGGREGATE_OPERATIONS
                and input_count != 1
            ):
                issues.append(
                    _issue(
                        PlanValidationCode.INVALID_CALCULATION,
                        f"{location}.inputs",
                        "aggregate calculation requires exactly one typed input",
                    )
                )
            if calculation.operation in {
                CalculationOperation.SUBTRACT,
                CalculationOperation.DATE_DIFFERENCE,
            } and input_count != 2:
                issues.append(
                    _issue(
                        PlanValidationCode.INVALID_CALCULATION,
                        f"{location}.inputs",
                        "binary calculation requires exactly two inputs",
                    )
                )
            if calculation.operation in {
                CalculationOperation.ADD,
                CalculationOperation.MULTIPLY,
            } and input_count < 2:
                issues.append(
                    _issue(
                        PlanValidationCode.INVALID_CALCULATION,
                        f"{location}.inputs",
                        "combining calculation requires at least two inputs",
                    )
                )
            if (
                calculation.operation == CalculationOperation.COMPOSITE_KEY
                and input_count < 2
            ):
                issues.append(
                    _issue(
                        PlanValidationCode.INVALID_CALCULATION,
                        f"{location}.inputs",
                        "composite key requires at least two typed inputs",
                    )
                )
            if calculation.operation in _WINDOW_OPERATIONS and input_count != 1:
                issues.append(
                    _issue(
                        PlanValidationCode.INVALID_CALCULATION,
                        f"{location}.inputs",
                        "window calculation requires exactly one input",
                    )
                )

            input_types = [
                available_types[ref]
                for ref in calculation.inputs
                if ref in available_types
            ]
            output_type = self._calculation_output_type(
                calculation.operation,
                input_types,
                input_count,
            )
            if len(input_types) != input_count or output_type is None:
                issues.append(
                    _issue(
                        PlanValidationCode.INVALID_CALCULATION,
                        f"{location}.inputs",
                        "calculation operation is incompatible with its input types",
                    )
                )
            else:
                available_types[calculation.id] = output_type

            if calculation.operation in _AGGREGATE_OPERATIONS or any(
                ref in aggregate for ref in calculation.inputs
            ):
                aggregate.add(calculation.id)
            ids.add(calculation.id)
            available.add(calculation.id)
        return ids, aggregate, available_types

    @staticmethod
    def _calculation_output_type(
        operation: CalculationOperation,
        input_types: Sequence[_LogicalType],
        input_count: int,
    ) -> _LogicalType | None:
        if len(input_types) != input_count or not input_types:
            return None
        if operation in _AGGREGATE_OPERATIONS and input_count != 1:
            return None
        if operation in {CalculationOperation.COUNT, CalculationOperation.COUNT_DISTINCT}:
            return _LogicalType.INTEGER
        if operation == CalculationOperation.SUM:
            if not all(item in _NUMERIC_TYPES for item in input_types):
                return None
            return (
                _LogicalType.DECIMAL
                if _LogicalType.DECIMAL in input_types
                else _LogicalType.INTEGER
            )
        if operation == CalculationOperation.AVERAGE:
            if all(item in _NUMERIC_TYPES for item in input_types):
                return _LogicalType.DECIMAL
            if len(input_types) == 1 and input_types[0] == _LogicalType.DURATION:
                return _LogicalType.DURATION
            return None
        if operation in {
            CalculationOperation.ADD,
            CalculationOperation.SUBTRACT,
            CalculationOperation.MULTIPLY,
        }:
            if not all(item in _NUMERIC_TYPES for item in input_types):
                return None
            return (
                _LogicalType.DECIMAL
                if _LogicalType.DECIMAL in input_types
                else _LogicalType.INTEGER
            )
        if operation == CalculationOperation.GROWTH:
            return (
                _LogicalType.DECIMAL
                if len(input_types) == 1 and input_types[0] in _NUMERIC_TYPES
                else None
            )
        if operation == CalculationOperation.LAG:
            return input_types[0] if len(input_types) == 1 else None
        if operation == CalculationOperation.DATE_DIFFERENCE:
            return (
                _LogicalType.DURATION
                if len(input_types) == 2
                and all(item in _TEMPORAL_TYPES for item in input_types)
                else None
            )
        if operation == CalculationOperation.COMPOSITE_KEY:
            return (
                _LogicalType.COMPOSITE
                if len(input_types) >= 2
                and all(item != _LogicalType.JSON for item in input_types)
                else None
            )
        return None

    @classmethod
    def _validate_predicate_types(
        cls,
        plan: LogicalQueryPlan,
        reference_types: Mapping[str, _LogicalType],
        issues: list[PlanValidationIssue],
    ) -> None:
        for location, predicates in (("filters", plan.filters), ("having", plan.having)):
            for index, predicate in enumerate(predicates):
                logical_type = reference_types.get(predicate.ref)
                if logical_type is None:
                    continue
                if not cls._predicate_matches_type(
                    logical_type,
                    predicate.operator,
                    predicate.value,
                ):
                    issues.append(
                        _issue(
                            PlanValidationCode.PREDICATE_TYPE_MISMATCH,
                            f"{location}[{index}]",
                            "predicate operator or literal is incompatible with its logical type",
                        )
                    )

    @classmethod
    def _predicate_matches_type(
        cls,
        logical_type: _LogicalType,
        operator: object,
        value: object,
    ) -> bool:
        if operator in {"is_null", "is_not_null"}:
            return value is None
        allowed_operators = {
            _LogicalType.STRING: {"eq", "neq", "in", "not_in", "contains"},
            _LogicalType.INTEGER: {"eq", "neq", "gt", "gte", "lt", "lte", "in", "not_in"},
            _LogicalType.DECIMAL: {"eq", "neq", "gt", "gte", "lt", "lte", "in", "not_in"},
            _LogicalType.DURATION: {"eq", "neq", "gt", "gte", "lt", "lte", "in", "not_in"},
            _LogicalType.BOOLEAN: {"eq", "neq", "in", "not_in"},
            _LogicalType.DATE: {"eq", "neq", "gt", "gte", "lt", "lte", "in", "not_in"},
            _LogicalType.DATETIME: {"eq", "neq", "gt", "gte", "lt", "lte", "in", "not_in"},
            _LogicalType.JSON: set(),
            _LogicalType.COMPOSITE: set(),
        }
        if operator not in allowed_operators[logical_type]:
            return False
        values = value if isinstance(value, tuple) else (value,)
        if operator in {"in", "not_in"} and not isinstance(value, tuple):
            return False
        if operator not in {"in", "not_in"} and isinstance(value, tuple):
            return False
        return all(cls._literal_matches_type(logical_type, item) for item in values)

    @staticmethod
    def _literal_matches_type(logical_type: _LogicalType, value: object) -> bool:
        if logical_type == _LogicalType.STRING:
            return isinstance(value, str)
        if logical_type == _LogicalType.INTEGER:
            return isinstance(value, int) and not isinstance(value, bool)
        if logical_type in {_LogicalType.DECIMAL, _LogicalType.DURATION}:
            return (
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and (not isinstance(value, float) or math.isfinite(value))
            )
        if logical_type == _LogicalType.BOOLEAN:
            return isinstance(value, bool)
        if logical_type == _LogicalType.DATE:
            if not isinstance(value, str) or "T" in value:
                return False
            try:
                date.fromisoformat(value)
            except ValueError:
                return False
            return True
        if logical_type == _LogicalType.DATETIME:
            if not isinstance(value, str):
                return False
            try:
                datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                return False
            return True
        return False

    @staticmethod
    def _validate_windows(
        plan: LogicalQueryPlan,
        fields: Mapping[str, object],
        calculation_ids: set[str],
        reserved_aliases: set[str],
        domain_name: str,
        issues: list[PlanValidationIssue],
    ) -> None:
        calculations = {
            calculation.id: calculation
            for calculation in plan.derived_calculations
        }
        windows_by_calculation: dict[str, list[int]] = {}
        window_ids: set[str] = set()
        for index, window in enumerate(plan.window_specs):
            location = f"window_specs[{index}]"
            if not window.id.startswith(f"{domain_name}."):
                issues.append(
                    _issue(
                        PlanValidationCode.INVALID_WINDOW,
                        f"{location}.id",
                        "window ID must use the DomainPack namespace",
                    )
                )
            if window.id in window_ids or window.id in reserved_aliases:
                issues.append(
                    _issue(
                        PlanValidationCode.DUPLICATE_REFERENCE,
                        f"{location}.id",
                        "window IDs must be unique",
                    )
                )
            window_ids.add(window.id)
            windows_by_calculation.setdefault(window.calculation, []).append(index)
            calculation = calculations.get(window.calculation)
            if window.calculation not in calculation_ids or calculation is None:
                issues.append(
                    _issue(
                        PlanValidationCode.INVALID_WINDOW,
                        f"{location}.calculation",
                        "window must reference a declared calculation",
                    )
                )
                continue
            if calculation.operation not in _WINDOW_OPERATIONS:
                issues.append(
                    _issue(
                        PlanValidationCode.INVALID_WINDOW,
                        f"{location}.calculation",
                        "window must reference a window calculation",
                    )
                )
            if window.partition_by != calculation.partition_by:
                issues.append(
                    _issue(
                        PlanValidationCode.INVALID_WINDOW,
                        f"{location}.partition_by",
                        "window partition must match its calculation",
                    )
                )
            if (
                plan.series_axis is None
                or window.axis_ref != plan.series_axis.field
                or not window.ordering
                or window.ordering[0].ref != window.axis_ref
                or window.axis_ref not in window.output_grain
            ):
                issues.append(
                    _issue(
                        PlanValidationCode.INVALID_WINDOW,
                        f"{location}.axis_ref",
                        "sequence window must bind its primary order to the plan series axis",
                    )
                )
            if set(window.partition_by) - set(fields):
                issues.append(
                    _issue(
                        PlanValidationCode.INVALID_WINDOW,
                        f"{location}.partition_by",
                        "window partitions must be declared fields",
                    )
                )
            if any(item.ref not in fields for item in window.ordering):
                issues.append(
                    _issue(
                        PlanValidationCode.INVALID_WINDOW,
                        f"{location}.ordering",
                        "window ordering must use declared fields",
                    )
                )
            if window.output_grain != plan.expected_grain:
                issues.append(
                    _issue(
                        PlanValidationCode.INVALID_WINDOW,
                        f"{location}.output_grain",
                        "window output grain must equal plan expected grain",
                    )
                )
        for calculation in plan.derived_calculations:
            if calculation.operation not in _WINDOW_OPERATIONS:
                continue
            indexes = windows_by_calculation.get(calculation.id, [])
            if len(indexes) != 1:
                issues.append(
                    _issue(
                        PlanValidationCode.WINDOW_REQUIRED,
                        f"derived_calculations.{calculation.id}",
                        "window calculation requires exactly one window spec",
                    )
                )

    @classmethod
    def _validate_analysis_semantics(
        cls,
        plan: LogicalQueryPlan,
        fields: Mapping[str, tuple[str, object]],
        reference_types: Mapping[str, _LogicalType],
        calculation_ids: set[str],
        aggregate_calculations: set[str],
        issues: list[PlanValidationIssue],
    ) -> None:
        aggregate_outputs = set(plan.metrics) | (
            calculation_ids & aggregate_calculations
        )

        def analysis_issue(path: str, message: str) -> None:
            issues.append(
                _issue(
                    PlanValidationCode.ANALYSIS_SEMANTICS_MISMATCH,
                    path,
                    message,
                )
            )

        axis = plan.series_axis
        if axis is not None:
            axis_type = reference_types.get(axis.field)
            if axis.field not in fields:
                analysis_issue("series_axis.field", "series axis must be a canonical field")
            if axis.field not in plan.dimensions or axis.field not in plan.expected_grain:
                analysis_issue(
                    "series_axis.field",
                    "series axis must be present in dimensions and expected grain",
                )
            if axis.kind == "time":
                if axis_type not in _TEMPORAL_TYPES:
                    analysis_issue(
                        "series_axis.field",
                        "time series axis must use a canonical date or datetime field",
                    )
                if (
                    plan.time_range is None
                    or plan.time_range.field != axis.field
                    or plan.time_grain != axis.time_grain
                ):
                    analysis_issue(
                        "series_axis",
                        "time series axis must match explicit time field and grain",
                    )
                time_ordered = any(item.ref == axis.field for item in plan.ordering) or any(
                    item.ref == axis.field
                    for window in plan.window_specs
                    for item in window.ordering
                )
                if not time_ordered:
                    analysis_issue(
                        "ordering",
                        "time series requires deterministic ordering on its time axis",
                    )
            elif axis.kind == "numeric":
                if axis_type not in _NUMERIC_TYPES:
                    analysis_issue(
                        "series_axis.field",
                        "numeric series axis must use a canonical numeric field",
                    )
                if plan.time_range is not None or plan.time_grain is not None:
                    analysis_issue(
                        "series_axis",
                        "numeric series axis cannot claim temporal selection",
                    )
                if not plan.ordering or plan.ordering[0].ref != axis.field:
                    analysis_issue(
                        "ordering",
                        "numeric series requires primary ordering on its axis",
                    )

        if plan.result_shape == ResultShape.TIME_SERIES and (
            axis is None or axis.kind != "time"
        ):
            analysis_issue(
                "result_shape",
                "time-series result requires an explicit temporal series axis",
            )
        if plan.analysis_type == AnalysisType.TREND:
            if axis is None:
                analysis_issue(
                    "analysis_type",
                    "trend analysis requires an explicit typed series axis",
                )
            if not aggregate_outputs or plan.fields:
                analysis_issue(
                    "analysis_type",
                    "trend analysis requires aggregate outputs without detail fields",
                )
        if plan.window_specs and axis is None:
            analysis_issue(
                "window_specs",
                "window analysis requires an explicit typed series axis",
            )

        ranking = plan.ranking
        limited_aggregate = (
            plan.result_shape != ResultShape.DETAIL
            and plan.limit is not None
            and bool(plan.ordering)
            and plan.ordering[0].ref in aggregate_outputs
        )
        if limited_aggregate and ranking is None:
            analysis_issue(
                "ranking",
                "limited aggregate result requires an explicit top-N specification",
            )
        if ranking is not None:
            if ranking.measure not in aggregate_outputs:
                analysis_issue(
                    "ranking.measure",
                    "ranking measure must be a requested metric or aggregate calculation",
                )
            if not plan.ordering or plan.ordering[0].ref != ranking.measure:
                analysis_issue(
                    "ordering",
                    "ranking primary ordering must match its declared measure",
                )
            if ranking.mode == "top_n" and plan.limit is None:
                analysis_issue("limit", "top-N ranking requires an explicit limit")
            if ranking.mode == "full" and plan.limit is not None:
                analysis_issue(
                    "ranking.mode",
                    "limited ranking must declare top-N mode",
                )
        if plan.result_shape == ResultShape.RANKING and ranking is None:
            analysis_issue(
                "ranking",
                "ranking result requires an explicit ranking specification",
            )
        if plan.analysis_type == AnalysisType.RANKING:
            if ranking is None or not aggregate_outputs or not plan.dimensions:
                analysis_issue(
                    "analysis_type",
                    "ranking analysis requires grouped aggregate output and ranking metadata",
                )

        if plan.result_shape == ResultShape.DETAIL:
            if not plan.fields or plan.metrics or bool(
                calculation_ids & aggregate_calculations
            ):
                analysis_issue(
                    "result_shape",
                    "detail results require row fields and cannot mix aggregate outputs",
                )
        if plan.analysis_type == AnalysisType.METRIC and (
            not aggregate_outputs or plan.fields
        ):
            analysis_issue(
                "analysis_type",
                "metric analysis requires aggregate outputs without detail fields",
            )
        if plan.analysis_type == AnalysisType.COMPARISON and (
            not aggregate_outputs or not plan.dimensions or plan.fields
        ):
            analysis_issue(
                "analysis_type",
                "comparison requires grouped aggregate outputs",
            )
        cross_tab = plan.cross_tab
        if (
            plan.analysis_type == AnalysisType.CROSS_TAB
            or plan.result_shape == ResultShape.CROSS_TAB
        ):
            if cross_tab is None:
                analysis_issue(
                    "cross_tab",
                    "cross-tab result requires explicit row and column axes",
                )
            else:
                axis_dimensions = (cross_tab.row_axis, cross_tab.column_axis)
                if len(plan.dimensions) < 2 or len(set(plan.dimensions)) < 2:
                    analysis_issue(
                        "dimensions",
                        "cross-tab requires at least two independent dimensions",
                    )
                if (
                    len(plan.dimensions) != 2
                    or set(axis_dimensions) != set(plan.dimensions)
                ):
                    analysis_issue(
                        "cross_tab",
                        "cross-tab axes must exactly cover plan dimensions",
                    )
                if set(cross_tab.values) != aggregate_outputs:
                    analysis_issue(
                        "cross_tab.values",
                        "cross-tab values must exactly match aggregate outputs",
                    )
        elif cross_tab is not None:
            analysis_issue(
                "cross_tab",
                "cross-tab axes require cross-tab analysis and result shape",
            )
        if plan.analysis_type == AnalysisType.CROSS_TAB and (
            not aggregate_outputs
            or len(plan.dimensions) < 2
            or len(set(plan.dimensions)) < 2
            or plan.fields
        ):
            analysis_issue(
                "analysis_type",
                "cross-tab requires two independent dimensions and an aggregate output",
            )
        if plan.analysis_type == AnalysisType.DISTRIBUTION and (
            not aggregate_outputs or not plan.dimensions or plan.fields
        ):
            analysis_issue(
                "analysis_type",
                "distribution requires a grouped aggregate output",
            )
        if plan.analysis_type == AnalysisType.DERIVED and not plan.derived_calculations:
            analysis_issue(
                "analysis_type",
                "derived analysis requires typed calculations",
            )

        seller_filter = any(
            predicate.ref in {
                "commerce.OrderItem.seller_id",
                "commerce.Seller.seller_id",
            }
            and predicate.operator == "eq"
            and predicate.value == "context.seller_id"
            for predicate in plan.filters
        )
        if plan.context.tenant_scope == "seller" and not seller_filter:
            issues.append(
                _issue(
                    PlanValidationCode.CONTEXT_MISMATCH,
                    "context.tenant_scope",
                    "seller scope requires an explicit canonical seller context filter",
                )
            )
        if plan.analysis_type == AnalysisType.TENANT_SCOPED and (
            plan.context.tenant_scope != "seller" or not seller_filter
        ):
            issues.append(
                _issue(
                    PlanValidationCode.CONTEXT_MISMATCH,
                    "analysis_type",
                    "tenant-scoped analysis requires seller context and filter",
                )
            )
        if plan.analysis_type == AnalysisType.FOLLOW_UP and (
            plan.context.mode != "follow_up"
            or plan.context.prior_question is None
            or not plan.context.preserve
        ):
            issues.append(
                _issue(
                    PlanValidationCode.CONTEXT_MISMATCH,
                    "context",
                    "follow-up analysis requires prior question and preservation contract",
                )
            )
        if plan.context.mode == "follow_up" and plan.analysis_type != AnalysisType.FOLLOW_UP:
            issues.append(
                _issue(
                    PlanValidationCode.CONTEXT_MISMATCH,
                    "context.mode",
                    "follow-up context requires follow-up analysis type",
                )
            )

    @staticmethod
    def _required_entities(
        plan: LogicalQueryPlan,
        fields: Mapping[str, tuple[str, object]],
        metrics: Mapping[str, object],
    ) -> set[str]:
        required: set[str] = set()
        for metric_name in plan.metrics:
            metric = metrics.get(metric_name)
            if metric is None:
                continue
            for field_ref in metric.inputs:
                if field_ref in fields:
                    required.add(fields[field_ref][0])
        refs: list[str] = [
            *plan.dimensions,
            *plan.fields,
            *plan.expected_grain,
        ]
        if plan.time_range is not None:
            refs.append(plan.time_range.field)
        if plan.series_axis is not None:
            refs.append(plan.series_axis.field)
        if plan.ranking is not None:
            refs.append(plan.ranking.measure)
        if plan.cross_tab is not None:
            refs.append(plan.cross_tab.row_axis)
            refs.append(plan.cross_tab.column_axis)
            refs.extend(plan.cross_tab.values)
        refs.extend(predicate.ref for predicate in plan.filters)
        refs.extend(predicate.ref for predicate in plan.having)
        refs.extend(ordering.ref for ordering in plan.ordering)
        for calculation in plan.derived_calculations:
            refs.extend(calculation.inputs)
            refs.extend(calculation.partition_by)
        for window in plan.window_specs:
            refs.append(window.axis_ref)
            refs.extend(window.partition_by)
            refs.extend(item.ref for item in window.ordering)
            refs.extend(window.output_grain)
        for alignment in plan.grain_alignment:
            refs.extend(alignment.join_grain)
        for ref in refs:
            if ref in fields:
                required.add(fields[ref][0])
            metric = metrics.get(ref)
            if metric is not None:
                for input_ref in metric.inputs:
                    if input_ref in fields:
                        required.add(fields[input_ref][0])
        return required

    @staticmethod
    def _validate_entity_declarations(
        plan: LogicalQueryPlan,
        required_entities: set[str],
        relationships: Sequence[CanonicalRelationship],
        issues: list[PlanValidationIssue],
    ) -> None:
        declared = set(plan.entities)
        for relationship in relationships:
            required_entities.add(relationship.from_entity)
            required_entities.add(relationship.to_entity)
        missing = required_entities - declared
        if missing:
            issues.append(
                _issue(
                    PlanValidationCode.ENTITY_DECLARATION_REQUIRED,
                    "entities",
                    "all referenced entities and relationship endpoints must be declared",
                )
            )
        known_declared = plan.entities
        if len(known_declared) < 2:
            return
        graph = _adjacency(relationships)
        reachable = {known_declared[0]}
        queue = deque([known_declared[0]])
        while queue:
            entity = queue.popleft()
            for step in graph.get(entity, ()):
                if step.target not in reachable:
                    reachable.add(step.target)
                    queue.append(step.target)
        if any(entity not in reachable for entity in known_declared[1:]):
            issues.append(
                _issue(
                    PlanValidationCode.UNREACHABLE_ENTITY,
                    "relationships",
                    "declared entities are not connected by declared relationships",
                )
            )

    @staticmethod
    def _validate_time(
        plan: LogicalQueryPlan,
        fields: Mapping[str, tuple[str, object]],
        metrics: Mapping[str, object],
        issues: list[PlanValidationIssue],
    ) -> None:
        time_range = plan.time_range
        if plan.time_grain is not None and time_range is None:
            issues.append(
                _issue(
                    PlanValidationCode.INCOMPLETE_TIME_RANGE,
                    "time_grain",
                    "time grain requires an explicit canonical time field",
                )
            )
            return
        if time_range is None:
            return
        field_entry = fields.get(time_range.field)
        if field_entry is None or field_entry[1].type not in {"date", "datetime"}:
            issues.append(
                _issue(
                    PlanValidationCode.INVALID_TIME_FIELD,
                    "time_range.field",
                    "time range field must be a declared date or datetime",
                )
            )
        if (time_range.start is None) != (time_range.end is None):
            issues.append(
                _issue(
                    PlanValidationCode.INCOMPLETE_TIME_RANGE,
                    "time_range",
                    "bounded time ranges require both start and end",
                )
            )
        elif time_range.start is None and plan.time_grain is None:
            issues.append(
                _issue(
                    PlanValidationCode.INCOMPLETE_TIME_RANGE,
                    "time_range",
                    "time selection requires a range or grain",
                )
            )
        elif time_range.start is not None and time_range.end is not None:
            try:
                start = datetime.fromisoformat(time_range.start.replace("Z", "+00:00"))
                end = datetime.fromisoformat(time_range.end.replace("Z", "+00:00"))
                valid_order = start < end
            except (TypeError, ValueError):
                valid_order = False
            if not valid_order:
                issues.append(
                    _issue(
                        PlanValidationCode.INCOMPLETE_TIME_RANGE,
                        "time_range",
                        "time range must use ordered ISO-8601 half-open bounds",
                    )
                )
        if plan.time_grain is not None and (
            time_range.field not in plan.dimensions
            or time_range.field not in plan.expected_grain
        ):
            issues.append(
                _issue(
                    PlanValidationCode.EXPECTED_GRAIN_MISMATCH,
                    "time_grain",
                    "time grain field must appear in dimensions and expected grain",
                )
            )
        for metric_name in plan.metrics:
            metric = metrics.get(metric_name)
            if (
                metric is not None
                and metric.event_time is not None
                and metric.event_time != time_range.field
            ):
                issues.append(
                    _issue(
                        PlanValidationCode.METRIC_TIME_FIELD_MISMATCH,
                        "time_range.field",
                        f"metric {metric_name!r} requires its declared event time",
                    )
                )

    @classmethod
    def _validate_order_limit_shape(
        cls,
        plan: LogicalQueryPlan,
        allowed_refs: set[str],
        entities: Mapping[str, object],
        relationships: Sequence[CanonicalRelationship],
        issues: list[PlanValidationIssue],
    ) -> None:
        ordering_refs = set(plan.dimensions) | set(plan.fields) | set(plan.expected_grain)
        ordering_refs |= set(plan.metrics) | {
            calculation.id for calculation in plan.derived_calculations
        }
        for index, ordering in enumerate(plan.ordering):
            if ordering.ref not in allowed_refs or ordering.ref not in ordering_refs:
                issues.append(
                    _issue(
                        PlanValidationCode.INVALID_ORDERING,
                        f"ordering[{index}].ref",
                        "ordering must reference a declared result output",
                    )
                )
        if len(plan.ordering) != len({item.ref for item in plan.ordering}):
            issues.append(
                _issue(
                    PlanValidationCode.DUPLICATE_REFERENCE,
                    "ordering",
                    "ordering references must be unique",
                )
            )
        expected_shape = expected_result_shape(plan)
        if plan.result_shape != expected_shape:
            issues.append(
                _issue(
                    PlanValidationCode.RESULT_SHAPE_MISMATCH,
                    "result_shape",
                    f"analysis semantics require result shape {expected_shape.value!r}",
                )
            )
        if expected_shape in {ResultShape.RANKING, ResultShape.TIME_SERIES} and not plan.ordering:
            issues.append(
                _issue(
                    PlanValidationCode.ORDERING_REQUIRED,
                    "ordering",
                    "ranking and series results require deterministic ordering",
                )
            )
        if plan.limit is not None and not plan.ordering:
            issues.append(
                _issue(
                    PlanValidationCode.ORDERING_REQUIRED,
                    "ordering",
                    "limited results require deterministic ordering",
                )
            )
        if expected_shape == ResultShape.DETAIL and plan.limit is None:
            issues.append(
                _issue(
                    PlanValidationCode.UNBOUNDED_DETAIL,
                    "limit",
                    "detail results require an explicit positive limit",
                )
            )

        effective_dimensions = _effective_dimensions(plan)
        if expected_shape == ResultShape.DETAIL:
            anchor_entities = cls._detail_anchor_entities(
                plan,
                entities,
                relationships,
            )
            anchor_grains = {
                tuple(f"{entity_id}.{field}" for field in entity.grain)
                for entity_id, entity in entities.items()
                if entity_id in anchor_entities
            }
            expected_grain_valid = (
                bool(anchor_entities)
                and plan.expected_grain in anchor_grains
            )
        else:
            expected_set = set(plan.expected_grain)
            expected_grain_valid = (
                set(effective_dimensions).issubset(expected_set)
                and expected_set.issubset(set(plan.dimensions))
                and plan.expected_grain
                == tuple(
                    dimension
                    for dimension in plan.dimensions
                    if dimension in expected_set
                )
            )
        if not expected_grain_valid:
            issues.append(
                _issue(
                    PlanValidationCode.EXPECTED_GRAIN_MISMATCH,
                    "expected_grain",
                    "expected grain does not match the logical result grain",
                )
            )

    @staticmethod
    def _detail_anchor_entities(
        plan: LogicalQueryPlan,
        entities: Mapping[str, object],
        relationships: Sequence[CanonicalRelationship],
    ) -> set[str]:
        field_entities = {
            f"{entity_id}.{field_name}": entity_id
            for entity_id, entity in entities.items()
            for field_name in entity.fields
        }
        calculations = {
            calculation.id: calculation
            for calculation in plan.derived_calculations
        }
        selected: set[str] = set()

        def add_ref(ref: str, visiting: frozenset[str] = frozenset()) -> None:
            entity = field_entities.get(ref)
            if entity is not None:
                selected.add(entity)
                return
            calculation = calculations.get(ref)
            if calculation is None or ref in visiting:
                return
            for input_ref in calculation.inputs:
                add_ref(input_ref, visiting | {ref})

        for ref in (*plan.fields, *plan.dimensions):
            add_ref(ref)
        for predicate in (*plan.filters, *plan.having):
            add_ref(predicate.ref)
        if plan.time_range is not None:
            add_ref(plan.time_range.field)
        for ordering in plan.ordering:
            add_ref(ordering.ref)
        for calculation in plan.derived_calculations:
            add_ref(calculation.id)
        for metric in plan.metrics:
            add_ref(metric)

        candidates: set[str] = set()
        for candidate in selected:
            paths = (
                _find_path(candidate, target, relationships)
                for target in selected
                if target != candidate
            )
            if all(
                path is not None and not any(step.expands for step in path)
                for path in paths
            ):
                candidates.add(candidate)
        return candidates

    def _validate_fanout(
        self,
        plan: LogicalQueryPlan,
        domain_pack: DomainPack,
        relationships: tuple[CanonicalRelationship, ...],
        fields: Mapping[str, tuple[str, object]],
        relationships_by_name: Mapping[str, CanonicalRelationship],
        issues: list[PlanValidationIssue],
    ) -> None:
        required: dict[
            tuple[str, str, tuple[str, ...]],
            tuple[bool, tuple[_PathStep, ...]],
        ] = {}
        for source, target, path, duplicate_safe in self._dangerous_paths(
            plan,
            domain_pack,
            relationships,
        ):
            has_many_to_many = any(step.many_to_many for step in path)
            if duplicate_safe and not has_many_to_many:
                continue
            key = (
                source,
                target,
                tuple(step.relationship.name for step in path),
            )
            required[key] = (duplicate_safe, path)

        alignments: dict[
            tuple[str, str, tuple[str, ...]],
            GrainAlignment,
        ] = {}
        for index, alignment in enumerate(plan.grain_alignment):
            location = f"grain_alignment[{index}]"
            identity = (
                alignment.source_entity,
                alignment.target_entity,
                alignment.relationship_path,
            )
            previous = alignments.get(identity)
            if previous is not None:
                if previous == alignment:
                    issues.append(
                        _issue(
                            PlanValidationCode.DUPLICATE_REFERENCE,
                            location,
                            "grain alignment identities must be unique",
                        )
                    )
                else:
                    issues.append(
                        _issue(
                            PlanValidationCode.UNSAFE_GRAIN_ALIGNMENT,
                            location,
                            "grain alignment identity has conflicting proofs",
                        )
                    )
            else:
                alignments[identity] = alignment
            path_relationships = tuple(
                relationships_by_name[name]
                for name in alignment.relationship_path
                if name in relationships_by_name
            )
            path = _find_path(
                alignment.source_entity,
                alignment.target_entity,
                path_relationships,
            )
            if (
                alignment.source_entity not in plan.entities
                or alignment.target_entity not in plan.entities
                or len(path_relationships) != len(alignment.relationship_path)
                or path is None
                or tuple(step.relationship.name for step in path)
                != alignment.relationship_path
                or alignment.join_grain != self._join_grain(path)
                or any(ref not in fields for ref in alignment.join_grain)
            ):
                issues.append(
                    _issue(
                        PlanValidationCode.UNSAFE_GRAIN_ALIGNMENT,
                        location,
                        "grain alignment must exactly describe a declared expanding path",
                    )
                )
            if identity not in required:
                issues.append(
                    _issue(
                        PlanValidationCode.UNSAFE_GRAIN_ALIGNMENT,
                        location,
                        "grain alignment must prove a required dangerous path",
                    )
                )

        for identity, (duplicate_safe, path) in required.items():
            source, target, _ = identity
            alignment = alignments.get(identity)
            strategy_valid = alignment is not None and (
                alignment.strategy == "pre_aggregate"
                or (duplicate_safe and alignment.strategy == "distinct")
            )
            if not strategy_valid:
                pair_path = f"grain_alignment.{source}.{target}"
                issues.append(
                    _issue(
                        PlanValidationCode.FANOUT_ALIGNMENT_REQUIRED,
                        pair_path,
                        "expanding measure path requires explicit safe alignment",
                    )
                )
                issues.append(
                    _issue(
                        PlanValidationCode.METRIC_GRAIN_INCOMPATIBLE,
                        pair_path,
                        "measure grain is incompatible with an expanding entity path",
                    )
                )

    def _dangerous_paths(
        self,
        plan: LogicalQueryPlan,
        domain_pack: DomainPack,
        relationships: Sequence[CanonicalRelationship],
    ) -> tuple[tuple[str, str, tuple[_PathStep, ...], bool], ...]:
        sources = self._measure_sources(plan, domain_pack)
        dangerous: dict[
            tuple[str, str, tuple[str, ...]],
            tuple[str, str, tuple[_PathStep, ...], bool],
        ] = {}
        for source in sources:
            for target in plan.entities:
                if target == source.entity:
                    continue
                path = _find_path(source.entity, target, relationships)
                if path is None or not any(step.expands for step in path):
                    continue
                key = (
                    source.entity,
                    target,
                    tuple(step.relationship.name for step in path),
                )
                current = dangerous.get(key)
                duplicate_safe = source.duplicate_safe
                if current is not None:
                    duplicate_safe = current[3] and duplicate_safe
                dangerous[key] = (
                    source.entity,
                    target,
                    path,
                    duplicate_safe,
                )
        return tuple(dangerous[key] for key in sorted(dangerous))

    @staticmethod
    def _measure_sources(
        plan: LogicalQueryPlan,
        domain_pack: DomainPack,
    ) -> tuple[_MeasureSource, ...]:
        fields_to_entity = {
            f"{entity_id}.{field_name}": entity_id
            for entity_id, entity in domain_pack.spec.entities.items()
            for field_name in entity.fields
        }
        metrics = domain_pack.spec.metrics
        calculations = {
            calculation.id: calculation
            for calculation in plan.derived_calculations
        }

        def ref_sources(ref: str, visiting: frozenset[str] = frozenset()) -> set[str]:
            if ref in fields_to_entity:
                return {fields_to_entity[ref]}
            metric = metrics.get(ref)
            if metric is not None:
                return {
                    fields_to_entity[input_ref]
                    for input_ref in metric.inputs
                    if input_ref in fields_to_entity
                }
            calculation = calculations.get(ref)
            if calculation is None or ref in visiting:
                return set()
            return set().union(
                *(ref_sources(item, visiting | {ref}) for item in calculation.inputs)
            )

        def is_aggregate(ref: str, visiting: frozenset[str] = frozenset()) -> bool:
            if ref in metrics:
                return True
            calculation = calculations.get(ref)
            if calculation is None or ref in visiting:
                return False
            if calculation.operation in _AGGREGATE_OPERATIONS:
                return True
            return any(
                is_aggregate(item, visiting | {ref})
                for item in calculation.inputs
            )

        def duplicate_safe(ref: str, visiting: frozenset[str] = frozenset()) -> bool:
            metric = metrics.get(ref)
            if metric is not None:
                return metric.aggregation == "count_distinct"
            calculation = calculations.get(ref)
            if calculation is None or ref in visiting:
                return False
            if calculation.operation == CalculationOperation.COUNT_DISTINCT:
                return True
            if calculation.operation in _WINDOW_OPERATIONS and len(calculation.inputs) == 1:
                return duplicate_safe(calculation.inputs[0], visiting | {ref})
            return False

        outputs: list[tuple[str, bool]] = []
        for metric_name in plan.metrics:
            for entity in ref_sources(metric_name):
                outputs.append((entity, duplicate_safe(metric_name)))
        for calculation in plan.derived_calculations:
            if not is_aggregate(calculation.id):
                continue
            for entity in ref_sources(calculation.id):
                outputs.append((entity, duplicate_safe(calculation.id)))
        combined: dict[str, bool] = {}
        for entity, safe in outputs:
            combined[entity] = combined.get(entity, True) and safe
        return tuple(
            _MeasureSource(entity=entity, duplicate_safe=combined[entity])
            for entity in sorted(combined)
        )

    @staticmethod
    def _join_grain(path: Sequence[_PathStep]) -> tuple[str, ...]:
        grain: list[str] = []
        for step in path:
            if not step.expands:
                continue
            relationship = step.relationship
            if relationship.cardinality == "many_to_one":
                entity = relationship.to_entity
                fields = relationship.to_fields
            elif relationship.cardinality == "one_to_many":
                entity = relationship.from_entity
                fields = relationship.from_fields
            elif step.source == relationship.from_entity:
                entity = relationship.from_entity
                fields = relationship.from_fields
            else:
                entity = relationship.to_entity
                fields = relationship.to_fields
            for field in fields:
                ref = f"{entity}.{field}"
                if ref not in grain:
                    grain.append(ref)
        return tuple(grain)
