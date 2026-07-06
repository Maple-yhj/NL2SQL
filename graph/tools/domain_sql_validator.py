from __future__ import annotations

import re
from typing import Any

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError


def validate_domain_sql(
    *,
    sql: str,
    constraints: dict[str, Any] | None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "ok": True,
        "violations": [],
        "warnings": [],
        "message": "success",
    }
    if not constraints:
        return result
    if not constraints.get("matched_rules"):
        return result

    try:
        expression = sqlglot.parse_one(sql.strip().rstrip(";"), read="postgres")
    except ParseError as exc:
        return {
            "ok": False,
            "violations": [_violation("domain_parse_error", str(exc))],
            "warnings": [],
            "message": "SQL domain validation failed.",
        }

    sql_text = expression.sql(dialect="postgres")
    tables = {table.name.casefold() for table in expression.find_all(exp.Table)}
    columns = {column.name.casefold() for column in expression.find_all(exp.Column)}
    select_text = _select_text(expression)
    group_text = _all_clause_text(expression, "group")
    order_text = _all_clause_text(expression, "order")

    for table in _string_list(constraints.get("required_tables")):
        if _name(table) not in tables:
            result["violations"].append(
                _violation("domain_missing_table", f"Required table is missing: {table}")
            )

    for table in _string_list(constraints.get("forbidden_tables")):
        if _name(table) in tables:
            result["violations"].append(
                _violation("domain_forbidden_table", f"Forbidden table is used: {table}")
            )

    for column in _string_list(constraints.get("required_columns")):
        if not _column_present(column, columns, sql_text):
            result["violations"].append(
                _violation("domain_missing_column", f"Required column is missing: {column}")
            )

    for column in _string_list(constraints.get("default_columns")):
        if not _projection_present(column, select_text):
            result["violations"].append(
                _violation(
                    "domain_missing_default_column",
                    f"Default detail column is missing from SELECT: {column}",
                )
            )

    if constraints.get("forbid_select_star") and _uses_select_star(expression):
        result["violations"].append(
            _violation("domain_select_star_forbidden", "SELECT * is not allowed for this query.")
        )

    for value in _string_list(constraints.get("required_filters")):
        if not _contains_fragment(sql_text, value):
            result["violations"].append(
                _violation("domain_missing_filter", f"Required filter is missing: {value}")
            )

    for value in _string_list(constraints.get("required_group_by")):
        if not group_text or not _contains_fragment(group_text, value):
            result["violations"].append(
                _violation("domain_missing_group_by", f"Required GROUP BY is missing: {value}")
            )

    for value in _string_list(constraints.get("required_order_by")):
        if not order_text or not _contains_fragment(order_text, value):
            result["violations"].append(
                _violation("domain_missing_order_by", f"Required ORDER BY is missing: {value}")
            )

    for value in _string_list(constraints.get("required_sql_fragments")):
        if not _contains_fragment(sql_text, value):
            result["violations"].append(
                _violation("domain_missing_sql_fragment", f"Required SQL fragment is missing: {value}")
            )

    for value in _string_list(constraints.get("forbidden_sql_fragments")):
        if _contains_fragment(sql_text, value):
            result["violations"].append(
                _violation("domain_forbidden_sql_fragment", f"Forbidden SQL fragment is used: {value}")
            )

    result["ok"] = not result["violations"]
    result["message"] = "success" if result["ok"] else "SQL domain validation failed."
    return result


def _violation(code: str, message: str) -> dict[str, str]:
    return {"code": code, "message": message}


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value if item is not None and str(item).strip()]
    return [str(value)]


def _name(value: str) -> str:
    return str(value).split(".")[-1].strip().casefold()


def _select_text(expression: exp.Expression) -> str:
    if not isinstance(expression, exp.Select):
        return ""
    return " ".join(item.sql(dialect="postgres") for item in expression.expressions)


def _clause_text(clause: Any) -> str:
    if clause is None:
        return ""
    return clause.sql(dialect="postgres") if hasattr(clause, "sql") else str(clause)


def _all_clause_text(expression: exp.Expression, clause_name: str) -> str:
    clauses: list[str] = []
    for select in expression.find_all(exp.Select):
        text = _clause_text(select.args.get(clause_name))
        if text:
            clauses.append(text)
    return " ".join(clauses)


def _uses_select_star(expression: exp.Expression) -> bool:
    return any(True for _ in expression.find_all(exp.Star))


def _column_present(column: str, columns: set[str], sql: str) -> bool:
    name = _name(column)
    return name in columns or _contains_fragment(sql, name)


def _projection_present(column: str, select_text: str) -> bool:
    if _contains_fragment(select_text, column):
        return True
    if _multiplication_alias_present(select_text, column):
        return True
    name = _name(column)
    return _contains_fragment(select_text, name)


def _contains_fragment(text: str, fragment: str) -> bool:
    raw_text = str(text or "")
    raw_fragment = str(fragment or "").strip()
    if not raw_fragment:
        return True

    text_compact = _compact(raw_text)
    fragment_compact = _compact(raw_fragment)
    if fragment_compact in text_compact:
        return True

    not_null_column = _not_null_column(raw_fragment)
    if not_null_column:
        column_name = _name(not_null_column)
        if (
            f"{column_name}isnotnull" in text_compact
            or f"not{column_name}isnull" in text_compact
        ):
            return True

    unqualified_fragment = _unqualify(raw_fragment)
    if unqualified_fragment != raw_fragment:
        unqualified_compact = _compact(unqualified_fragment)
        if unqualified_compact in text_compact:
            return True

    if _multiplication_alias_present(raw_text, raw_fragment):
        return True

    function_match = re.fullmatch(
        r"([a-z_][a-z0-9_]*)\((distinct)?([a-z_][a-z0-9_]*)\)",
        fragment_compact,
    )
    if function_match:
        function_name, distinct_token, column_name = function_match.groups()
        has_function = f"{function_name}(" in text_compact
        has_distinct = not distinct_token or "distinct" in text_compact
        return has_function and has_distinct and column_name in text_compact

    direction_match = re.fullmatch(r"([a-z_][a-z0-9_]*)(asc|desc)", fragment_compact)
    if direction_match:
        column_name, direction = direction_match.groups()
        return column_name in text_compact and direction in text_compact

    return False


def _compact(value: str) -> str:
    return re.sub(r"\s+", "", value).casefold()


def _unqualify(value: str) -> str:
    return re.sub(r"\b[A-Za-z_][A-Za-z0-9_]*\.", "", value)


def _not_null_column(value: str) -> str:
    match = re.fullmatch(
        r"\s*([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?)\s+IS\s+NOT\s+NULL\s*",
        str(value or ""),
        flags=re.IGNORECASE,
    )
    return match.group(1) if match else ""


def _multiplication_alias_present(text: str, fragment: str) -> bool:
    match = re.search(
        r"(.+?)\s+AS\s+([A-Za-z_][A-Za-z0-9_]*)\s*$",
        str(fragment or ""),
        flags=re.IGNORECASE,
    )
    if not match or "*" not in match.group(1):
        return False

    expression_text, alias = match.groups()
    factors = [
        _name(value)
        for value in re.findall(
            r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?",
            expression_text,
        )
    ]
    if len(factors) < 2:
        return False

    compact_text = _compact(text)
    return f"as{alias.casefold()}" in compact_text and all(
        factor.casefold() in compact_text for factor in factors
    )
