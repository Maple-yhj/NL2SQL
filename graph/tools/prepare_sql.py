from __future__ import annotations

from typing import Any

from graph.tools.domain_sql_validator import validate_domain_sql
from graph.tools.tenant_scope import apply_tenant_scope
from graph.tools.validate_sql import validate_sql


async def prepare_sql(
    *,
    sql: str,
    tenant_id: str,
    allowed_tables: list[str] | None = None,
    max_limit: int = 1000,
    constraints: dict[str, Any] | None = None,
) -> dict[str, Any]:
    validation = await validate_sql(
        sql=sql,
        tenant_id=tenant_id,
        allowed_tables=allowed_tables,
        max_limit=max_limit,
    )
    result: dict[str, Any] = {
        **validation,
        "valid": bool(validation.get("ok")),
        "logical_sql": str(validation.get("normalized_sql", "")),
        "tenant_scoped_sql": "",
        "executable_sql": "",
        "validation": validation,
        "domain_validation": {},
    }

    if validation.get("ok") is not True:
        return result

    domain_result = validate_domain_sql(
        sql=str(validation.get("normalized_sql", "")),
        constraints=constraints,
    )
    result["domain_validation"] = domain_result
    if domain_result.get("ok") is not True:
        result["ok"] = False
        result["valid"] = False
        result["message"] = str(domain_result.get("message") or "SQL domain validation failed.")
        result["violations"] = [
            *(validation.get("violations", []) or []),
            *(domain_result.get("violations", []) or []),
        ]
        result["warnings"] = [
            *(validation.get("warnings", []) or []),
            *(domain_result.get("warnings", []) or []),
        ]
        return result

    executable_sql = apply_tenant_scope(
        str(validation.get("normalized_sql", "")),
        tenant_id=tenant_id,
    )
    result["normalized_sql"] = executable_sql
    result["tenant_scoped_sql"] = executable_sql
    result["executable_sql"] = executable_sql
    result["valid"] = True
    return result
