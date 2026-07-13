from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from functools import reduce
import json
import operator
from pathlib import Path
from typing import Any, Iterable, Protocol


DATASET_PATH = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "olist_relational_dataset.json"
)


@dataclass(frozen=True)
class EvaluatedTable:
    columns: tuple[str, ...]
    rows: tuple[tuple[Any, ...], ...]


class PrincipalAuthority(Protocol):
    tenant_id: str
    roles: tuple[str, ...]


@dataclass(frozen=True)
class EvaluationPrincipal:
    tenant_id: str
    roles: tuple[str, ...]


def principal_is_admin(
    enterprise_binding: Any,
    principal: PrincipalAuthority,
) -> bool:
    allowed_roles = set(
        enterprise_binding.spec.policies.tenant_scope.admin_bypass.allowed_roles
    )
    return bool(allowed_roles.intersection(principal.roles))


def load_bound_relational_rows(
    enterprise_binding: Any,
    *,
    path: Path = DATASET_PATH,
) -> dict[str, tuple[dict[str, Any], ...]]:
    """Load physical test rows and map them through the Enterprise binding.

    The fixture deliberately stores physical OList table/column names.  This
    conversion is the evaluator's only schema adapter; missing bound tables or
    columns fail closed instead of silently becoming fabricated values.
    """

    physical = json.loads(path.read_text(encoding="utf-8"))
    bindings = enterprise_binding.spec.bindings
    expected_relations = {binding.relation for binding in bindings.values()}
    if set(physical) != expected_relations:
        raise ValueError("relational fixture must cover exactly the bound OList relations")

    canonical: dict[str, tuple[dict[str, Any], ...]] = {}
    for entity, binding in bindings.items():
        rows: list[dict[str, Any]] = []
        for physical_row in physical[binding.relation]:
            copies = int(physical_row.get("__copies", 1))
            for copy_index in range(copies):
                row: dict[str, Any] = {}
                for field, field_binding in binding.fields.items():
                    if field_binding.column is not None:
                        if field_binding.column not in physical_row:
                            raise ValueError(
                                f"fixture row omits {binding.relation}.{field_binding.column}"
                            )
                        row[field] = physical_row[field_binding.column]
                    else:
                        row[field] = field_binding.value
                if copies > 1:
                    first_grain_field = binding.grain[0]
                    row[first_grain_field] = f"{row[first_grain_field]}-{copy_index + 1:03d}"
                rows.append(row)
        canonical[entity] = tuple(rows)
    return canonical


def _predicate_parameter_values(
    predicates: Iterable[Any],
    *,
    principal: PrincipalAuthority,
) -> list[tuple[str, Any]]:
    values: list[tuple[str, Any]] = []
    for predicate in predicates:
        if predicate.operator in {"is_null", "is_not_null"}:
            continue
        purpose = "filter"
        value = predicate.value
        if value == "context.seller_id":
            purpose = "tenant_context"
            value = principal.tenant_id
        if predicate.operator in {"in", "not_in"}:
            values.extend((purpose, item) for item in value)
        else:
            values.append((purpose, value))
    return values


def derive_parameter_purpose_values(
    case: Any,
    enterprise_binding: Any,
    *,
    principal: PrincipalAuthority,
) -> tuple[tuple[str, Any], ...]:
    """Interpret raw eval operations into SQL parameter purposes and values.

    This intentionally has no dependency on the planner, binding compiler, SQL
    compiler, or their parameter objects.  It derives the semantic multiset;
    the SQL oracle separately freezes compiler positions and logical types.
    """

    admin = principal_is_admin(enterprise_binding, principal)
    values: list[tuple[str, Any]] = []
    has_context_filter = any(
        predicate.value == "context.seller_id" for predicate in case.filters
    )
    deferred_grain = bool(
        case.time is not None
        and case.time.grain is not None
        and case.time.start is None
        and case.time.end is None
        and has_context_filter
    )
    date_difference = any(
        calculation.operation == "date_difference" for calculation in case.calculations
    )
    early_scope = bool(
        not admin
        and date_difference
        and "commerce.Seller" in case.expected_entities
        and (case.time is None or case.time.grain is None)
    )

    if case.time is not None and case.time.grain is not None and not deferred_grain:
        values.append(("algorithm_constant", case.time.grain))
    if early_scope:
        values.append(("tenant_scope", principal.tenant_id))
    for calculation in case.calculations:
        if calculation.operation == "date_difference":
            values.extend(
                (("algorithm_constant", "epoch"), ("algorithm_constant", 86400))
            )
        elif calculation.operation == "growth":
            values.append(("algorithm_constant", 0))

    values.extend(_predicate_parameter_values(case.filters, principal=principal))
    if case.time is not None:
        if case.time.start is not None:
            values.append(("time_start", case.time.start))
        if case.time.end is not None:
            values.append(("time_end", case.time.end))

    if not admin and not early_scope:
        values.append(("tenant_scope", principal.tenant_id))

    if deferred_grain:
        values.append(("algorithm_constant", case.time.grain))

    values.extend(_predicate_parameter_values(case.having, principal=principal))
    max_rows = enterprise_binding.spec.policies.max_rows
    values.append(("limit", case.limit if case.limit is not None else max_rows))
    return tuple(values)


def assert_oracle_parameters_match(
    case: Any,
    enterprise_binding: Any,
    oracle: dict[str, Any],
    *,
    principal: PrincipalAuthority,
) -> None:
    expected = derive_parameter_purpose_values(
        case,
        enterprise_binding,
        principal=principal,
    )
    observed = tuple(
        (item.get("purpose"), item.get("value")) for item in oracle["parameters"]
    )
    if observed != expected:
        raise ValueError("oracle parameter purpose/value mismatch")


def _field_for_physical_column(binding: Any, physical_column: str) -> str:
    matches = tuple(
        field
        for field, field_binding in binding.fields.items()
        if field_binding.column == physical_column
    )
    if len(matches) != 1:
        raise ValueError(
            f"relationship column {binding.relation}.{physical_column} is not uniquely bound"
        )
    return matches[0]


def _required_entities(
    case: Any,
    enterprise_binding: Any,
    *,
    principal: PrincipalAuthority,
) -> set[str]:
    required = set(case.expected_entities)
    if not principal_is_admin(enterprise_binding, principal):
        ownership = enterprise_binding.spec.policies.tenant_scope.ownership_paths
        relationships = enterprise_binding.spec.relationships
        for entity in tuple(required):
            for relationship_id in ownership[entity]:
                relationship = relationships[relationship_id]
                required.add(relationship.from_entity)
                required.add(relationship.to_entity)
    return required


def _join_required_rows(
    case: Any,
    enterprise_binding: Any,
    rows_by_entity: dict[str, tuple[dict[str, Any], ...]],
    *,
    principal: PrincipalAuthority,
) -> list[dict[str, dict[str, Any]]]:
    required = _required_entities(
        case,
        enterprise_binding,
        principal=principal,
    )
    bindings = enterprise_binding.spec.bindings
    relationships = enterprise_binding.spec.relationships
    start = case.expected_entities[0]
    contexts = [{start: row} for row in rows_by_entity[start]]
    joined = {start}

    while joined != required:
        selected = None
        for relationship in relationships.values():
            endpoints = {relationship.from_entity, relationship.to_entity}
            if not endpoints.issubset(required):
                continue
            if len(endpoints & joined) == 1:
                selected = relationship
                break
        if selected is None:
            missing = ", ".join(sorted(required - joined))
            raise ValueError(f"fixture relationship graph cannot reach: {missing}")

        if selected.from_entity in joined:
            present_entity = selected.from_entity
            new_entity = selected.to_entity
            present_columns = selected.from_columns
            new_columns = selected.to_columns
        else:
            present_entity = selected.to_entity
            new_entity = selected.from_entity
            present_columns = selected.to_columns
            new_columns = selected.from_columns
        present_fields = tuple(
            _field_for_physical_column(bindings[present_entity], column)
            for column in present_columns
        )
        new_fields = tuple(
            _field_for_physical_column(bindings[new_entity], column)
            for column in new_columns
        )
        expanded: list[dict[str, dict[str, Any]]] = []
        for context in contexts:
            left_key = tuple(context[present_entity][field] for field in present_fields)
            for row in rows_by_entity[new_entity]:
                right_key = tuple(row[field] for field in new_fields)
                if left_key == right_key:
                    expanded.append({**context, new_entity: row})
        contexts = expanded
        joined.add(new_entity)

    if not principal_is_admin(enterprise_binding, principal):
        contexts = [
            context
            for context in contexts
            if context["commerce.Seller"]["seller_id"] == principal.tenant_id
        ]
    return contexts


def _as_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))


def _direct_ref_value(context: dict[str, dict[str, Any]], ref: str) -> Any:
    entity, field = ref.rsplit(".", 1)
    return context[entity][field]


def _row_calculation_value(
    context: dict[str, dict[str, Any]],
    ref: str,
    calculations: dict[str, Any],
) -> Any:
    calculation = calculations.get(ref)
    if calculation is None:
        return _direct_ref_value(context, ref)
    inputs = [
        _row_calculation_value(context, input_ref, calculations)
        for input_ref in calculation.inputs
    ]
    if calculation.operation == "subtract":
        return inputs[0] - inputs[1]
    if calculation.operation == "multiply":
        return reduce(operator.mul, inputs, 1)
    if calculation.operation == "date_difference":
        return (_as_datetime(inputs[0]) - _as_datetime(inputs[1])).total_seconds() / 86400
    if calculation.operation == "composite_key":
        return tuple(inputs)
    raise ValueError(f"{calculation.operation} is not a row-level operation")


def _predicate_matches(
    value: Any,
    predicate: Any,
    *,
    principal: PrincipalAuthority,
) -> bool:
    expected = predicate.value
    if expected == "context.seller_id":
        expected = principal.tenant_id
    operations = {
        "eq": operator.eq,
        "neq": operator.ne,
        "gt": operator.gt,
        "gte": operator.ge,
        "lt": operator.lt,
        "lte": operator.le,
        "in": lambda left, right: left in right,
        "not_in": lambda left, right: left not in right,
        "is_null": lambda left, _right: left is None,
        "is_not_null": lambda left, _right: left is not None,
    }
    return operations[predicate.operator](value, expected)


def _filter_detail_contexts(
    contexts: Iterable[dict[str, dict[str, Any]]],
    case: Any,
    *,
    principal: PrincipalAuthority,
) -> list[dict[str, dict[str, Any]]]:
    calculations = {item.id: item for item in case.calculations}
    filtered: list[dict[str, dict[str, Any]]] = []
    for context in contexts:
        if not all(
            _predicate_matches(
                _row_calculation_value(context, predicate.ref, calculations),
                predicate,
                principal=principal,
            )
            for predicate in case.filters
        ):
            continue
        if case.time is not None:
            value = _as_datetime(_direct_ref_value(context, case.time.field))
            if case.time.start is not None and value < _as_datetime(case.time.start):
                continue
            if case.time.end is not None and value >= _as_datetime(case.time.end):
                continue
        filtered.append(context)
    return filtered


def _terminal_calculations(case: Any) -> tuple[Any, ...]:
    consumed = {
        input_ref
        for calculation in case.calculations
        for input_ref in calculation.inputs
        if input_ref in {item.id for item in case.calculations}
    }
    return tuple(item for item in case.calculations if item.id not in consumed)


def _unique_aliases(refs: Iterable[str]) -> tuple[str, ...]:
    counts: Counter[str] = Counter()
    aliases: list[str] = []
    for ref in refs:
        base = ref.rsplit(".", 1)[-1]
        counts[base] += 1
        aliases.append(base if counts[base] == 1 else f"{base}_{counts[base]}")
    return tuple(aliases)


def _sortable(value: Any) -> tuple[bool, Any]:
    return (value is None, value)


def _evaluate_detail_case(
    case: Any,
    contexts: list[dict[str, dict[str, Any]]],
) -> EvaluatedTable:
    calculations = {item.id: item for item in case.calculations}
    for ordering in reversed(case.ordering):
        contexts.sort(
            key=lambda context, ref=ordering.ref: _sortable(
                _row_calculation_value(context, ref, calculations)
            ),
            reverse=ordering.direction == "desc",
        )

    deduplicated: list[dict[str, dict[str, Any]]] = []
    seen: set[str] = set()
    for context in contexts:
        grain = tuple(_direct_ref_value(context, ref) for ref in case.expected_grain)
        marker = json.dumps(grain, ensure_ascii=False, default=str)
        if marker not in seen:
            seen.add(marker)
            deduplicated.append(context)
    contexts = deduplicated[: case.limit or len(deduplicated)]

    output_refs = tuple(case.expected_fields) + tuple(
        item.id for item in _terminal_calculations(case)
    )
    columns = _unique_aliases(output_refs)
    rows = tuple(
        tuple(_row_calculation_value(context, ref, calculations) for ref in output_refs)
        for context in contexts
    )
    return EvaluatedTable(columns=columns, rows=rows)


def _dimension_value(
    context: dict[str, dict[str, Any]],
    ref: str,
    case: Any,
) -> Any:
    value = _direct_ref_value(context, ref)
    if case.time is None or case.time.grain is None or ref != case.time.field:
        return value
    timestamp = _as_datetime(value)
    if case.time.grain == "month":
        return timestamp.replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()
    if case.time.grain == "year":
        return timestamp.replace(
            month=1,
            day=1,
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        ).isoformat()
    raise ValueError(f"unsupported time grain: {case.time.grain}")


def _direct_entities_for_ref(ref: str, calculations: dict[str, Any]) -> set[str]:
    calculation = calculations.get(ref)
    if calculation is None:
        entity, _field = ref.rsplit(".", 1)
        return {entity}
    entities: set[str] = set()
    for input_ref in calculation.inputs:
        entities.update(_direct_entities_for_ref(input_ref, calculations))
    return entities


def _deduplicate_for_refs(
    contexts: Iterable[dict[str, dict[str, Any]]],
    refs: Iterable[str],
    domain_pack: Any,
    calculations: dict[str, Any],
) -> list[dict[str, dict[str, Any]]]:
    entities: set[str] = set()
    for ref in refs:
        entities.update(_direct_entities_for_ref(ref, calculations))
    seen: set[str] = set()
    result: list[dict[str, dict[str, Any]]] = []
    for context in contexts:
        grain_parts: list[Any] = []
        for entity in sorted(entities):
            for field in domain_pack.spec.entities[entity].grain:
                grain_parts.append(context[entity][field])
        marker = json.dumps(grain_parts, ensure_ascii=False, default=str)
        if marker not in seen:
            seen.add(marker)
            result.append(context)
    return result


def _aggregate_values(operation: str, values: list[Any]) -> Any:
    non_null = [value for value in values if value is not None]
    if operation == "sum":
        return sum(non_null)
    if operation == "average":
        return sum(non_null) / len(non_null) if non_null else None
    if operation == "count":
        return len(non_null)
    if operation == "count_distinct":
        return len(
            {
                json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
                for value in non_null
            }
        )
    raise ValueError(f"unsupported aggregate operation: {operation}")


def _metric_value(
    metric_ref: str,
    contexts: list[dict[str, dict[str, Any]]],
    domain_pack: Any,
    calculations: dict[str, Any],
) -> Any:
    metric = domain_pack.spec.metrics[metric_ref]
    unique_contexts = _deduplicate_for_refs(
        contexts,
        metric.inputs,
        domain_pack,
        calculations,
    )
    values: list[Any] = []
    for context in unique_contexts:
        inputs = [_direct_ref_value(context, ref) for ref in metric.inputs]
        if metric.combine == "add":
            values.append(sum(inputs))
        elif metric.combine in {None, "identity"} and len(inputs) == 1:
            values.append(inputs[0])
        else:
            raise ValueError(f"unsupported metric combination: {metric_ref}")
    return _aggregate_values(metric.aggregation, values)


def _aggregate_calculation_value(
    calculation_ref: str,
    contexts: list[dict[str, dict[str, Any]]],
    domain_pack: Any,
    calculations: dict[str, Any],
) -> Any:
    calculation = calculations[calculation_ref]
    if calculation.operation == "growth":
        raise ValueError("growth is evaluated after grouped aggregates")
    if calculation.operation not in {"sum", "average", "count", "count_distinct"}:
        if len(contexts) != 1:
            raise ValueError(f"row calculation {calculation_ref} has no aggregate")
        return _row_calculation_value(contexts[0], calculation_ref, calculations)
    unique_contexts = _deduplicate_for_refs(
        contexts,
        calculation.inputs,
        domain_pack,
        calculations,
    )
    values: list[Any] = []
    for context in unique_contexts:
        inputs = [
            _row_calculation_value(context, input_ref, calculations)
            for input_ref in calculation.inputs
        ]
        values.append(inputs[0] if len(inputs) == 1 else tuple(inputs))
    return _aggregate_values(calculation.operation, values)


def _record_value(record: dict[str, Any], ref: str) -> Any:
    if ref not in record:
        raise ValueError(f"grouped result does not contain {ref}")
    return record[ref]


def _apply_growth(
    records: list[dict[str, Any]],
    case: Any,
) -> None:
    for calculation in case.calculations:
        if calculation.operation != "growth":
            continue
        partitions: dict[str, list[dict[str, Any]]] = {}
        for record in records:
            key = tuple(record[ref] for ref in calculation.partition_by)
            marker = json.dumps(key, ensure_ascii=False, default=str)
            partitions.setdefault(marker, []).append(record)
        time_refs = [
            ref for ref in case.expected_dimensions if ref not in calculation.partition_by
        ]
        for partition in partitions.values():
            partition.sort(
                key=lambda record: tuple(_sortable(record[ref]) for ref in time_refs)
            )
            previous = None
            for record in partition:
                current = record[calculation.inputs[0]]
                record[calculation.id] = (
                    None
                    if previous in {None, 0}
                    else (current - previous) / abs(previous)
                )
                previous = current


def _sort_records(records: list[dict[str, Any]], case: Any) -> None:
    for ordering in reversed(case.ordering):
        descending = ordering.direction == "desc"

        def key(record: dict[str, Any], ref: str = ordering.ref) -> tuple[bool, Any]:
            value = _record_value(record, ref)
            if descending:
                return (value is not None, value)
            return (value is None, value)

        records.sort(key=key, reverse=descending)


def _evaluate_aggregate_case(
    case: Any,
    contexts: list[dict[str, dict[str, Any]]],
    domain_pack: Any,
    enterprise_binding: Any,
    *,
    principal: PrincipalAuthority,
) -> EvaluatedTable:
    calculations = {item.id: item for item in case.calculations}
    groups: dict[str, tuple[tuple[Any, ...], list[dict[str, dict[str, Any]]]]] = {}
    for context in contexts:
        key = tuple(_dimension_value(context, ref, case) for ref in case.expected_dimensions)
        marker = json.dumps(key, ensure_ascii=False, default=str)
        if marker not in groups:
            groups[marker] = (key, [])
        groups[marker][1].append(context)
    if not case.expected_dimensions and not groups:
        groups["[]"] = ((), [])

    records: list[dict[str, Any]] = []
    for dimension_values, grouped_contexts in groups.values():
        record = dict(zip(case.expected_dimensions, dimension_values, strict=True))
        for metric_ref in case.expected_metrics:
            record[metric_ref] = _metric_value(
                metric_ref,
                grouped_contexts,
                domain_pack,
                calculations,
            )
        for calculation in case.calculations:
            if calculation.operation != "growth" and calculation.operation in {
                "sum",
                "average",
                "count",
                "count_distinct",
            }:
                record[calculation.id] = _aggregate_calculation_value(
                    calculation.id,
                    grouped_contexts,
                    domain_pack,
                    calculations,
                )
        records.append(record)

    _apply_growth(records, case)
    records = [
        record
        for record in records
        if all(
            _predicate_matches(
                record[predicate.ref],
                predicate,
                principal=principal,
            )
            for predicate in case.having
        )
    ]
    _sort_records(records, case)
    max_rows = enterprise_binding.spec.policies.max_rows
    records = records[: case.limit if case.limit is not None else max_rows]

    output_refs = (
        tuple(case.expected_dimensions)
        + tuple(case.expected_fields)
        + tuple(case.expected_metrics)
        + tuple(item.id for item in _terminal_calculations(case))
    )
    return EvaluatedTable(
        columns=_unique_aliases(output_refs),
        rows=tuple(tuple(record[ref] for ref in output_refs) for record in records),
    )


def evaluate_raw_case(
    case: Any,
    domain_pack: Any,
    enterprise_binding: Any,
    *,
    principal: PrincipalAuthority,
) -> EvaluatedTable:
    """Evaluate a raw commerce eval against the independent relational fixture."""

    rows_by_entity = load_bound_relational_rows(enterprise_binding)
    contexts = _join_required_rows(
        case,
        enterprise_binding,
        rows_by_entity,
        principal=principal,
    )
    contexts = _filter_detail_contexts(
        contexts,
        case,
        principal=principal,
    )
    has_aggregate = bool(case.expected_metrics) or any(
        item.operation in {"sum", "average", "count", "count_distinct", "growth"}
        for item in case.calculations
    )
    if not case.expected_dimensions and not has_aggregate:
        return _evaluate_detail_case(case, contexts)
    return _evaluate_aggregate_case(
        case,
        contexts,
        domain_pack,
        enterprise_binding,
        principal=principal,
    )
