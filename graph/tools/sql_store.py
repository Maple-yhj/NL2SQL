from __future__ import annotations

from typing import Any

from core.embeddings import EmbeddingClientProtocol
from rag.vector_store import search_semantic_index


async def search_metrics(
    query: str,
    tenant_id: str,
    embedding_client: EmbeddingClientProtocol,
    top_k: int = 3,
    min_score: float | None = None,
) -> dict[str, Any]:
    if not query.strip():
        raise ValueError("Tool[search_metrics]: query is empty")
    if not tenant_id.strip():
        raise ValueError("Tool[search_metrics]: tenant_id is empty")
    if not 1 <= top_k <= 20:
        raise ValueError("Tool[search_metrics]: top_k must be between 1 and 20")

    query_embedding = await embedding_client.embed_text(query)
    hits = await search_semantic_index(
        query_embedding=query_embedding,
        tenant_id=tenant_id,
        object_types=["metric"],
        top_k=top_k,
    )
    metrics = []
    for hit in hits:
        if min_score is not None and hit.similarity < min_score:
            continue
        metadata = hit.metadata
        metrics.append(
            {
                "metric_name": metadata.get("metric_name"),
                "display_name": metadata.get("display_name"),
                "business_def": metadata.get("business_def"),
                "sql_expr": metadata.get("sql_expr"),
                "base_table": metadata.get("base_table"),
                "time_column": metadata.get("time_column"),
                "dimensions": metadata.get("dimensions", []),
                "filters": metadata.get("filters", []),
                "join_tables": metadata.get("join_tables", []),
                "forbidden": metadata.get("forbidden", []),
                "synonyms": metadata.get("synonyms", []),
                "score": hit.similarity,
            }
        )
    return {
        "ok": True,
        "query": query,
        "tenant_id": tenant_id,
        "metrics": metrics,
        "message": "success" if metrics else "No matching metrics found.",
    }


async def search_schema(
    query: str,
    tenant_id: str,
    embedding_client: EmbeddingClientProtocol,
    top_k: int = 8,
    object_types: tuple[str, ...] = ("table", "column"),
    table_names: list[str] | None = None,
    min_score: float | None = None,
) -> dict[str, Any]:
    if not query.strip():
        raise ValueError("Tool[search_schema]: query is empty")
    if not tenant_id.strip():
        raise ValueError("Tool[search_schema]: tenant_id is empty")
    if not 1 <= top_k <= 20:
        raise ValueError("Tool[search_schema]: top_k must be between 1 and 20")
    invalid_types = set(object_types) - {"table", "column"}
    if invalid_types:
        raise ValueError(f"Tool[search_schema]: invalid object_types: {sorted(invalid_types)}")

    query_embedding = await embedding_client.embed_text(query)
    hits = await search_semantic_index(
        query_embedding=query_embedding,
        tenant_id=tenant_id,
        object_types=object_types,
        top_k=top_k,
    )
    selected_tables = set(table_names or [])
    schema_by_table: dict[str, dict[str, Any]] = {}

    for hit in hits:
        metadata = hit.metadata
        table_name = metadata.get("table_name")
        if not table_name:
            continue
        if selected_tables and table_name not in selected_tables:
            continue
        if min_score is not None and hit.similarity < min_score:
            continue

        table_entry = schema_by_table.setdefault(
            table_name,
            {
                "table_name": table_name,
                "table_comment": "",
                "score": hit.similarity,
                "columns": {},
            },
        )
        table_entry["score"] = max(table_entry["score"], hit.similarity)

        if hit.object_type == "table":
            table_entry["table_comment"] = metadata.get("comment", "")
            continue
        if hit.object_type != "column":
            continue

        column_name = metadata.get("column_name")
        if not column_name:
            continue
        columns = table_entry["columns"]
        column = {
            "column_name": column_name,
            "data_type": metadata.get("data_type"),
            "nullable": metadata.get("nullable"),
            "default": metadata.get("default"),
            "comment": metadata.get("comment", ""),
            "sample_values": metadata.get("sample_values", []),
            "score": hit.similarity,
        }
        existing = columns.get(column_name)
        if existing is None or existing.get("score", 0) < hit.similarity:
            columns[column_name] = column

    for table_entry in schema_by_table.values():
        columns = table_entry["columns"]
        table_entry["columns"] = sorted(
            columns.values(),
            key=lambda item: item.get("score", 0),
            reverse=True,
        )

    schema = sorted(
        schema_by_table.values(),
        key=lambda item: item["score"],
        reverse=True,
    )
    return {
        "ok": True,
        "query": query,
        "tenant_id": tenant_id,
        "schema": schema,
        "message": "success" if schema else "No matching schema found.",
    }
