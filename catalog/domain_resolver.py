from __future__ import annotations

import re
from typing import Any

from catalog.domain_models import (
    DomainCalculatedField,
    DomainMetric,
    DomainProfile,
    DomainQueryRule,
    DomainResolution,
    DomainTerm,
)
from engine.models import QueryIntent


_JOIN_TABLE_RE = re.compile(r"\bJOIN\s+([A-Za-z_][A-Za-z0-9_\.]*)", re.IGNORECASE)


def resolve_domain_context(
    *,
    profile: DomainProfile,
    question: str,
    intent: QueryIntent | None = None,
    plan: Any = None,
    metrics_result: dict[str, Any] | None = None,
) -> DomainResolution:
    text = _combined_text(question=question, intent=intent, plan=plan)
    metric_tables = _metric_table_names(metrics_result or {})
    profile_metric_tables = [table for table in metric_tables if table in profile.tables]
    has_non_profile_metric_scope = bool(metric_tables and not profile_metric_tables)

    required_tables: list[str] = []
    optional_tables: list[str] = []
    required_columns: list[str] = []
    matched_terms: list[str] = []
    matched_rules: list[str] = []
    metric_hints: list[str] = []
    calculation_hints: list[str] = []
    required_filters: list[str] = []
    default_columns: list[str] = []
    sql_hints: list[str] = []
    required_group_by: list[str] = []
    required_order_by: list[str] = []
    required_sql_fragments: list[str] = []
    forbidden_sql_fragments: list[str] = []
    forbidden_tables: list[str] = []
    suppressed_required_tables: list[str] = []
    forbid_select_star = False

    for metric in profile.metrics.values():
        if _metric_matches(metric, text, intent) and not has_non_profile_metric_scope:
            _append_unique(required_tables, metric.base_table)
            for table in metric.required_tables:
                _append_unique(required_tables, table)
            hint = f"{metric.name}: {metric.expression}"
            if metric.time_column:
                hint += f" | time column: {metric.time_column}"
            _append_unique(metric_hints, hint)

    for table in profile_metric_tables:
        _append_unique(required_tables, table)

    for term in profile.terms.values():
        if _term_matches(term, text):
            _append_unique(matched_terms, term.name)
            for table in term.required_tables:
                _append_unique(required_tables, table)
            for column in term.columns:
                _append_unique(required_columns, column)

    for field in profile.calculated_fields.values():
        if _calculated_field_matches(field, text):
            for table in field.required_tables:
                _append_unique(required_tables, table)
            _append_unique(calculation_hints, f"{field.name}: {field.expression}")

    for rule in profile.query_rules.values():
        if _query_rule_matches(rule, text):
            _append_unique(matched_rules, rule.name)
            for table in rule.required_tables:
                _append_unique(required_tables, table)
            for table in rule.optional_tables:
                _append_unique(optional_tables, table)
            for table in rule.suppressed_required_tables:
                _append_unique(suppressed_required_tables, table)
            for column in rule.required_columns:
                _append_unique(required_columns, column)
            for value in rule.required_filters:
                _append_unique(required_filters, value)
            for value in rule.required_group_by:
                _append_unique(required_group_by, value)
            for value in rule.required_order_by:
                _append_unique(required_order_by, value)
            for value in rule.required_sql_fragments:
                _append_unique(required_sql_fragments, value)
            for value in rule.forbidden_sql_fragments:
                _append_unique(forbidden_sql_fragments, value)
            for value in rule.forbidden_tables:
                _append_unique(forbidden_tables, value)
            for column in rule.default_columns:
                _append_unique(default_columns, column)
            for hint in rule.sql_hints:
                _append_unique(sql_hints, hint)
            forbid_select_star = forbid_select_star or rule.forbid_select_star

    if has_non_profile_metric_scope and not matched_terms and not profile_metric_tables:
        return DomainResolution(
            domain_id=profile.domain_id,
            display_name=profile.display_name,
        )

    if forbidden_tables or suppressed_required_tables:
        forbidden_table_names = {table.split(".")[0] for table in forbidden_tables}
        suppressed_table_names = {table.split(".")[0] for table in suppressed_required_tables}
        excluded_table_names = forbidden_table_names | suppressed_table_names
        required_tables = [
            table for table in required_tables if table.split(".")[0] not in excluded_table_names
        ]

    join_hints = _join_hints(profile, required_tables)
    tenant_scope_hints = []
    if profile.tenant_scope.description:
        tenant_scope_hints.append(profile.tenant_scope.description)
    elif profile.tenant_scope.tenant_column:
        tenant_scope_hints.append(f"Tenant scope column: {profile.tenant_scope.tenant_column}")

    return DomainResolution(
        domain_id=profile.domain_id,
        display_name=profile.display_name,
        required_tables=required_tables,
        optional_tables=optional_tables,
        required_columns=required_columns,
        join_hints=join_hints,
        metric_hints=metric_hints,
        calculation_hints=calculation_hints,
        tenant_scope_hints=tenant_scope_hints if required_tables else [],
        matched_terms=matched_terms,
        matched_rules=matched_rules,
        required_filters=required_filters,
        default_columns=default_columns,
        sql_hints=sql_hints,
        required_group_by=required_group_by,
        required_order_by=required_order_by,
        required_sql_fragments=required_sql_fragments,
        forbidden_sql_fragments=forbidden_sql_fragments,
        forbidden_tables=forbidden_tables,
        forbid_select_star=forbid_select_star,
    )


def format_domain_context(resolution: DomainResolution | None) -> str:
    if resolution is None or not (
        resolution.required_tables
        or resolution.optional_tables
        or resolution.required_columns
        or resolution.join_hints
        or resolution.metric_hints
        or resolution.calculation_hints
        or resolution.required_filters
        or resolution.default_columns
        or resolution.sql_hints
        or resolution.required_group_by
        or resolution.required_order_by
        or resolution.required_sql_fragments
        or resolution.forbidden_sql_fragments
        or resolution.forbidden_tables
        or resolution.forbid_select_star
    ):
        return ""

    lines = [f"DOMAIN: {resolution.display_name} ({resolution.domain_id})"]
    if resolution.matched_rules:
        lines.append("Hard domain rules: " + ", ".join(resolution.matched_rules))
    if resolution.required_tables:
        lines.append("Required tables: " + ", ".join(resolution.required_tables))
    if resolution.optional_tables:
        lines.append("Optional tables: " + ", ".join(resolution.optional_tables))
    if resolution.required_columns:
        lines.append("Required columns: " + ", ".join(resolution.required_columns))
    if resolution.required_filters:
        lines.append("Required filters:")
        lines.extend(f"- {item}" for item in resolution.required_filters)
    if resolution.required_group_by:
        lines.append("Required GROUP BY:")
        lines.extend(f"- {item}" for item in resolution.required_group_by)
    if resolution.required_order_by:
        lines.append("Required ORDER BY:")
        lines.extend(f"- {item}" for item in resolution.required_order_by)
    if resolution.required_sql_fragments:
        lines.append("Required SQL fragments:")
        lines.extend(f"- {item}" for item in resolution.required_sql_fragments)
    if resolution.forbidden_sql_fragments:
        lines.append("Forbidden SQL fragments:")
        lines.extend(f"- {item}" for item in resolution.forbidden_sql_fragments)
    if resolution.forbidden_tables:
        lines.append("Forbidden tables:")
        lines.extend(f"- {item}" for item in resolution.forbidden_tables)
    if resolution.forbid_select_star:
        lines.append("Forbidden projection: SELECT *")
    if resolution.default_columns:
        lines.append("Default detail columns:")
        lines.extend(f"- {item}" for item in resolution.default_columns)
    if resolution.sql_hints:
        lines.append("SQL shape hints:")
        lines.extend(f"- {item}" for item in resolution.sql_hints)
    if resolution.metric_hints:
        lines.append("Metric rules:")
        lines.extend(f"- {hint}" for hint in resolution.metric_hints)
    if resolution.calculation_hints:
        lines.append("Calculated fields:")
        lines.extend(f"- {hint}" for hint in resolution.calculation_hints)
    if resolution.join_hints:
        lines.append("Join hints:")
        lines.extend(f"- {hint}" for hint in resolution.join_hints)
    if resolution.tenant_scope_hints:
        lines.append("Tenant scope:")
        lines.extend(f"- {hint}" for hint in resolution.tenant_scope_hints)
    return "\n".join(lines)


def _combined_text(*, question: str, intent: QueryIntent | None, plan: Any) -> str:
    parts = [question]
    if intent is not None:
        parts.extend(intent.metrics)
        parts.extend(intent.dimensions)
        parts.extend(intent.filters)
        parts.extend(str(value) for value in intent.time_range.values())
    if plan is not None:
        if isinstance(plan, dict):
            parts.append(str(plan))
        else:
            parts.append(str(plan))
    return "\n".join(part for part in parts if part).casefold()


def _metric_matches(metric: DomainMetric, text: str, intent: QueryIntent | None) -> bool:
    names = {metric.name.casefold(), *(alias.casefold() for alias in metric.aliases)}
    if intent is not None and any(value.casefold() in names for value in intent.metrics):
        return True
    return any(_contains_token(text, value) for value in names)


def _term_matches(term: DomainTerm, text: str) -> bool:
    values = [term.name, *term.aliases, *term.columns]
    return any(_contains_token(text, value.casefold()) for value in values if value)


def _calculated_field_matches(field: DomainCalculatedField, text: str) -> bool:
    values = [field.name, *field.aliases]
    return any(_contains_token(text, value.casefold()) for value in values if value)


def _query_rule_matches(rule: DomainQueryRule, text: str) -> bool:
    if any(_contains_token(text, value.casefold()) for value in rule.match_none if value):
        return False
    values = [rule.name, *rule.aliases]
    if any(_contains_token(text, value.casefold()) for value in values if value):
        return True
    return bool(rule.match_all) and all(
        _contains_token(text, value.casefold()) for value in rule.match_all if value
    )


def _contains_token(text: str, token: str) -> bool:
    value = token.casefold().strip()
    return bool(value and value in text)


def _metric_table_names(metrics_result: dict[str, Any]) -> list[str]:
    names: list[str] = []
    for metric in metrics_result.get("metrics", []) or []:
        base_table = str(metric.get("base_table") or "").strip()
        if base_table:
            _append_unique(names, base_table.split()[0].split(".")[-1])
        for join_clause in metric.get("join_tables", []) or []:
            for match in _JOIN_TABLE_RE.finditer(str(join_clause)):
                _append_unique(names, match.group(1).split(".")[-1])
    return names


def _join_hints(profile: DomainProfile, required_tables: list[str]) -> list[str]:
    selected = set(required_tables)
    hints: list[str] = []
    for join in profile.joins:
        left_table = join.left.split(".")[0]
        right_table = join.right.split(".")[0]
        if left_table in selected and right_table in selected:
            hint = f"{join.left} = {join.right}"
            if join.description:
                hint += f" ({join.description})"
            hints.append(hint)
    return hints


def _append_unique(items: list[str], value: str) -> None:
    text = str(value or "").strip()
    if text and text not in items:
        items.append(text)
