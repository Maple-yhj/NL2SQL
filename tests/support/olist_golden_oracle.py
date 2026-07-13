from __future__ import annotations

from typing import Any

import sqlglot
from sqlglot import exp

from tests.support.olist_relational_evaluator import (
    PrincipalAuthority,
    assert_oracle_parameters_match,
    evaluate_raw_case,
    principal_is_admin,
)


def _sql(node: exp.Expression | None) -> str | None:
    if node is None:
        return None
    return node.sql(dialect="postgres", pretty=False, normalize=True).casefold()


def sql_semantic_signature(sql: str) -> dict[str, Any]:
    statement = sqlglot.parse_one(sql, read="postgres")
    if not isinstance(statement, exp.Select) or statement.find(exp.Union) is not None:
        raise ValueError("golden SQL must be one SELECT")
    group = statement.args.get("group")
    order = statement.args.get("order")
    limit = statement.args.get("limit")
    return {
        "projections": [_sql(item) for item in statement.expressions],
        "aggregates": sorted(
            _sql(item) for item in statement.find_all(exp.AggFunc)
        ),
        "filters": sorted(_sql(item.this) for item in statement.find_all(exp.Where)),
        "having": sorted(_sql(item.this) for item in statement.find_all(exp.Having)),
        "grouping": [
            _sql(item) for item in (group.expressions if group is not None else ())
        ],
        "relations": sorted(
            f"{table.db}.{table.name}" if table.db else table.name
            for table in statement.find_all(exp.Table)
        ),
        "referenced_columns": sorted(
            {column.name.casefold() for column in statement.find_all(exp.Column)}
        ),
        "joins": [
            list(item)
            for item in sorted(
                (
                    str(join.args.get("kind") or "inner").casefold(),
                    _sql(join.this),
                    _sql(join.args.get("on")),
                )
                for join in statement.find_all(exp.Join)
            )
        ],
        "ordering": [
            {
                "expression": _sql(item.this),
                "descending": bool(item.args.get("desc")),
            }
            for item in (order.expressions if order is not None else ())
        ],
        "limit": _sql(limit.expression if limit is not None else None),
    }


def assert_sql_semantics(sql: str, expected: dict[str, Any]) -> None:
    observed = sql_semantic_signature(sql)
    if observed != expected:
        differing = sorted(
            key for key in set(observed) | set(expected) if observed.get(key) != expected.get(key)
        )
        raise ValueError("SQL semantic oracle mismatch: " + ", ".join(differing))


def assert_plan_matches_raw_case(plan: Any, case: Any) -> None:
    expected_analysis_type = (
        "comparison"
        if case.analysis_type == "cross_tab" and len(case.expected_dimensions) < 2
        else case.analysis_type
    )
    raw_ordering = tuple((item.ref, item.direction) for item in case.ordering)
    plan_ordering = tuple((item.ref, item.direction) for item in plan.ordering)
    composite_inputs = {
        item.id: tuple(item.inputs)
        for item in plan.derived_calculations
        if str(item.operation) == "composite_key"
    }
    normalized_plan_calculations = tuple(
        (
            item.id,
            item.operation,
            composite_inputs.get(item.inputs[0], tuple(item.inputs))
            if str(item.operation) == "count_distinct" and len(item.inputs) == 1
            else tuple(item.inputs),
            tuple(item.partition_by),
        )
        for item in plan.derived_calculations
        if str(item.operation) != "composite_key"
    )
    expected = {
        "analysis_type": expected_analysis_type,
        "metrics": tuple(case.expected_metrics),
        "entities": tuple(case.expected_entities),
        "dimensions": tuple(case.expected_dimensions),
        "fields": tuple(case.expected_fields),
        "expected_grain": tuple(case.expected_grain),
        "filters": tuple(
            (item.ref, item.operator, item.value) for item in case.filters
        ),
        "having": tuple(
            (item.ref, item.operator, item.value) for item in case.having
        ),
        "ordering": raw_ordering,
        "limit": case.limit,
        "time_range": (
            None
            if case.time is None
            else (case.time.field, case.time.start, case.time.end)
        ),
        "time_grain": None if case.time is None else case.time.grain,
        "context": (
            case.context.mode,
            case.context.tenant_scope,
            case.context.prior_question,
            tuple(case.context.preserve),
        ),
        "calculations": tuple(
            (item.id, item.operation, tuple(item.inputs), tuple(item.partition_by))
            for item in case.calculations
        ),
    }
    observed = {
        "analysis_type": plan.analysis_type,
        "metrics": tuple(plan.metrics),
        "entities": tuple(plan.entities),
        "dimensions": tuple(plan.dimensions),
        "fields": tuple(plan.fields),
        "expected_grain": tuple(plan.expected_grain),
        "filters": tuple((item.ref, item.operator, item.value) for item in plan.filters),
        "having": tuple((item.ref, item.operator, item.value) for item in plan.having),
        "ordering": plan_ordering[: len(raw_ordering)],
        "limit": case.limit if case.limit is None else plan.limit,
        "time_range": (
            None
            if plan.time_range is None
            else (plan.time_range.field, plan.time_range.start, plan.time_range.end)
        ),
        "time_grain": plan.time_grain,
        "context": (
            plan.context.mode,
            plan.context.tenant_scope,
            plan.context.prior_question,
            tuple(plan.context.preserve),
        ),
        "calculations": normalized_plan_calculations,
    }
    differing = sorted(key for key in expected if observed[key] != expected[key])
    if differing:
        raise ValueError("logical plan/raw eval mismatch: " + ", ".join(differing))


def assert_oracle_matches_governed_sources(
    case: Any,
    domain_pack: Any,
    enterprise_binding: Any,
    oracle: dict[str, Any],
    *,
    principal: PrincipalAuthority,
) -> None:
    assert_oracle_parameters_match(
        case,
        enterprise_binding,
        oracle,
        principal=principal,
    )
    evaluated = evaluate_raw_case(
        case,
        domain_pack,
        enterprise_binding,
        principal=principal,
    )
    if tuple(oracle["columns"]) != evaluated.columns:
        raise ValueError("oracle columns do not match independent relational evaluation")
    if tuple(tuple(row) for row in oracle["rows"]) != evaluated.rows:
        raise ValueError("oracle rows do not match independent relational evaluation")
    signature = oracle["signature"]
    relations = set(signature["relations"])
    direct_relations = {
        enterprise_binding.spec.bindings[entity].relation
        for entity in case.expected_entities
    }
    if not direct_relations.issubset(relations):
        raise ValueError("oracle omits an enterprise-bound relation")

    referenced_columns = set(signature["referenced_columns"])
    calculations = {item.id: item for item in case.calculations}

    def require_ref(ref: str) -> None:
        entity, field = ref.rsplit(".", 1)
        entity_binding = enterprise_binding.spec.bindings.get(entity)
        if entity_binding is None or field not in entity_binding.fields:
            calculation = calculations.get(ref)
            if calculation is None:
                raise ValueError(f"oracle cannot resolve governed ref: {ref}")
            for input_ref in calculation.inputs:
                require_ref(input_ref)
            return
        binding = entity_binding.fields[field]
        if binding.column is not None and binding.column.casefold() not in referenced_columns:
            raise ValueError(f"oracle omits enterprise-bound field: {ref}")

    aggregation_tokens = {
        "sum": "sum(",
        "average": "avg(",
        "count_distinct": "count(distinct ",
    }
    aggregate_sql = " ".join(signature["aggregates"])
    for metric_id in case.expected_metrics:
        metric = domain_pack.spec.metrics[metric_id]
        token = aggregation_tokens[metric.aggregation]
        if token not in aggregate_sql:
            raise ValueError(f"oracle omits domain aggregation: {metric_id}")
        for ref in metric.inputs:
            require_ref(ref)
    for ref in (*case.expected_dimensions, *case.expected_fields):
        require_ref(ref)
    for predicate in (*case.filters, *case.having):
        require_ref(predicate.ref)
    for calculation in case.calculations:
        for ref in calculation.inputs:
            if ref.count(".") >= 2:
                require_ref(ref)
    if case.time is not None:
        require_ref(case.time.field)
    if (
        case.filters
        or case.time is not None
        or not principal_is_admin(enterprise_binding, principal)
    ) and not signature["filters"]:
        raise ValueError("oracle omits required filters/time/seller scope")
    if case.having and not signature["having"]:
        raise ValueError("oracle omits requested HAVING semantics")
    if case.ordering and len(signature["ordering"]) < len(case.ordering):
        raise ValueError("oracle omits requested ordering")
    if case.limit is not None and signature["limit"] is None:
        raise ValueError("oracle omits requested limit")
    if case.expected_dimensions and signature["aggregates"] and not signature["grouping"]:
        raise ValueError("oracle omits requested grouping/grain")
