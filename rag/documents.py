from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from engine.metrics import Metric


@dataclass(frozen=True, slots=True)
class EmbeddingDocument:
    tenant_id: str
    object_type: str
    object_key: str
    source_table: str | None
    source_id: int | None
    content: str
    metadata: dict[str, Any]

    def content_hash(self) -> str:
        return hashlib.sha256(self.content.encode("utf-8")).hexdigest()


def safe_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value]
    return [str(value)]


def join_text(value: Any) -> str:
    items = safe_list(value)
    return ", ".join(items) if items else "none"


def build_metric_metadata(metric: Metric) -> dict[str, Any]:
    return {
        "metric_name": metric.name,
        "display_name": metric.display_name,
        "business_def": metric.business_def,
        "sql_expr": metric.sql_expr,
        "base_table": metric.base_table,
        "join_tables": safe_list(metric.join_tables),
        "time_column": metric.time_column,
        "dimensions": safe_list(metric.dimensions),
        "filters": safe_list(metric.filters),
        "forbidden": safe_list(metric.forbidden),
        "synonyms": safe_list(metric.synonyms),
    }


def build_metric_content(metric: Metric) -> str:
    content = f"""
        Metric: {metric.name}
        Display name: {metric.display_name}
        Business definition: {metric.business_def}
        SQL expression: {metric.sql_expr}
        Base table: {metric.base_table}
        Join tables: {join_text(metric.join_tables)}
        Time column: {metric.time_column}
        Dimensions: {join_text(metric.dimensions)}
        Default filters: {join_text(metric.filters)}
        Forbidden conditions: {join_text(metric.forbidden)}
        Synonyms: {join_text(metric.synonyms)}
        """.strip()

    return content


def build_metric_document(metric: Metric) -> EmbeddingDocument:
    tenant_id = getattr(metric, "tenant_id", "demo")
    source_id = getattr(metric, "id", None)

    return EmbeddingDocument(
        tenant_id=tenant_id,
        object_type="metric",
        object_key=f"metric:{metric.name}",
        source_table="metrics_registry",
        source_id=source_id,
        content=build_metric_content(metric),
        metadata=build_metric_metadata(metric),
    )

def build_table_metadata(table: dict) -> dict[str, Any]:
    columns = table.get("columns") or []
    return {
        "table_name": table.get("table"),
        "comment": table.get("comment") or "",
        "column_count": len(columns),
        "columns": [col.get("name") for col in columns if col.get("name")],
    }

def build_table_content(table: dict) -> str:
    columns = table.get("columns") or []
    column_lines = []
    for col in columns:
        nullable = "nullable" if col.get("nullable") else "not null"
        comment = col.get("comment") or ""
        column_lines.append(
            f"- {col.get('name')} ({col.get('type')}, {nullable}): {comment}"
        )

    return f"""
Table: {table.get("table")}
Comment: {table.get("comment") or ""}
Columns:
{chr(10).join(column_lines)}
""".strip()

def build_table_document(table: dict, tenant_id: str = "demo") -> EmbeddingDocument:
    table_name = table["table"]
    return EmbeddingDocument(
        tenant_id=tenant_id,
        object_type="table",
        object_key=f"table:{table_name}",
        source_table="schema_catalog",
        source_id=None,
        content=build_table_content(table),
        metadata=build_table_metadata(table),
    )

def build_column_metadata(table: dict, column: dict) -> dict[str, Any]:
    return {
        "table_name": table.get("table"),
        "column_name": column.get("name"),
        "data_type": column.get("type"),
        "nullable": bool(column.get("nullable")),
        "default": column.get("default"),
        "comment": column.get("comment") or "",
        "sample_values": safe_list(column.get("sample_values")),
    }

def build_column_content(table: dict, column: dict) -> str:
    sample_values = join_text(column.get("sample_values"))
    nullable = "nullable" if column.get("nullable") else "not null"

    return f"""
Column: {table.get("table")}.{column.get("name")}
Table: {table.get("table")}
Table comment: {table.get("comment") or ""}
Data type: {column.get("type")}
Nullable: {nullable}
Default: {column.get("default") or "none"}
Comment: {column.get("comment") or ""}
Sample values: {sample_values}
""".strip()


def build_column_document(
    table: dict,
    column: dict,
    tenant_id: str = "demo",
) -> EmbeddingDocument:
    table_name = table["table"]
    column_name = column["name"]

    return EmbeddingDocument(
        tenant_id=tenant_id,
        object_type="column",
        object_key=f"column:{table_name}.{column_name}",
        source_table="schema_catalog",
        source_id=None,
        content=build_column_content(table, column),
        metadata=build_column_metadata(table, column),
    )

def build_schema_documents(
    catalog: list[dict],
    tenant_id: str = "demo",
) -> list[EmbeddingDocument]:
    documents: list[EmbeddingDocument] = []

    for table in catalog:
        documents.append(build_table_document(table, tenant_id=tenant_id))

        for column in table.get("columns") or []:
            documents.append(
                build_column_document(table, column, tenant_id=tenant_id)
            )

    return documents