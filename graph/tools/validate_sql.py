from typing import Any

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError


DANGEROUS_EXPRESSIONS = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Drop,
    exp.Alter,
    exp.Create,
    exp.TruncateTable,
    exp.Merge,
)


def _violation(code: str, message: str) -> dict[str, str]:
    return {"code": code, "message": message}


def _set_limit(expression: Any, limit_value: int) -> None:
    expression.set("limit", exp.Limit(expression=exp.Literal.number(limit_value)))


async def validate_sql(
    sql: str,
    tenant_id: str,
    allowed_tables: list[str] | None = None,
    max_limit: int = 1000,
) -> dict[str, Any]:
    """Validate and normalize a read-only query within an authorized table scope."""

    result: dict[str, Any] = {
        "ok": False,
        "sql": sql,
        "normalized_sql": "",
        "tenant_id": tenant_id,
        "tables": [],
        "columns": [],
        "limit": None,
        "violations": [],
        "warnings": [],
        "message": "",
    }

    if not sql or not sql.strip():
        raise ValueError("Tool[validate_sql]: arg 'sql' is empty")
    if not tenant_id or not tenant_id.strip():
        raise ValueError("Tool[validate_sql]: arg 'tenant_id' is empty")
    if max_limit <= 0:
        raise ValueError("Tool[validate_sql]: arg 'max_limit' must be positive")
    if not allowed_tables:
        result["violations"].append(
            _violation(
                "missing_allowed_tables",
                "SQL validation requires a non-empty authorized table scope.",
            )
        )
        result["message"] = "SQL table authorization scope is missing."
        return result

    raw_sql = sql.strip().rstrip(";").strip()

    try:
        statements = [stmt for stmt in sqlglot.parse(raw_sql, read="postgres") if stmt]
    except ParseError as exc:
        
        result["violations"].append(_violation("parse_error", str(exc)))
        result["message"] = "SQL parse failed."
        return result

    if len(statements) != 1:
        result["violations"].append(
            _violation("multiple_statements", "Only one SQL statement is allowed.")
        )
        result["message"] = "SQL contains multiple statements."
        return result

    expression = statements[0]

    if not isinstance(expression, exp.Select):
        result["violations"].append(
            _violation("not_select", "Only SELECT or WITH ... SELECT queries are allowed.")
        )

    if any(expression.find(node_type) for node_type in DANGEROUS_EXPRESSIONS):
        result["violations"].append(
            _violation("not_readonly", "DDL, DML, and mutation statements are not allowed.")
        )

    cte_names = {
        str(cte.alias or "").casefold()
        for cte in expression.find_all(exp.CTE)
        if cte.alias
    }
    tables = sorted(
        {
            table.name
            for table in expression.find_all(exp.Table)
            if table.name.casefold() not in cte_names
        }
    )
    result["tables"] = tables

    allowed = {table.lower() for table in allowed_tables}
    disallowed = [table for table in tables if table.lower() not in allowed]
    if disallowed:
        result["violations"].append(
            _violation(
                "table_not_allowed",
                f"SQL uses tables outside allowed_tables: {', '.join(disallowed)}",
            )
        )

    result["columns"] = sorted({column.name for column in expression.find_all(exp.Column)})

    limit = expression.args.get("limit")
    if limit is None:
        _set_limit(expression, max_limit)
        result["limit"] = max_limit
        result["warnings"].append(f"LIMIT was missing and has been set to {max_limit}.")
    else:
        try:
            current_limit = int(limit.expression.name)
            if current_limit > max_limit:
                limit.set("expression", exp.Literal.number(max_limit))
                result["limit"] = max_limit
                result["warnings"].append(f"LIMIT was reduced from {current_limit} to {max_limit}.")
            else:
                result["limit"] = current_limit
        except (TypeError, ValueError, AttributeError):
            result["violations"].append(
                _violation("invalid_limit", "LIMIT must be a numeric literal.")
            )

    result["normalized_sql"] = expression.sql(dialect="postgres")
    result["ok"] = not result["violations"]
    result["message"] = "success" if result["ok"] else "SQL validation failed."
    return result
