from rag.embedding_client import GeminiEmbeddingClient
from rag.vector_store import SearchHit
from rag.vector_store import search_semantic_index


async def search_metrics(
    query: str,
    tenant_id: str,
    top_k: int = 3,
    min_score: float | None = None,
) -> dict:
    '''
    '''

    if top_k <= 0 or top_k >20:
        top_k = 5

    result : dict = {
        "ok": True,
        "query": query,
        "tenant_id": tenant_id,
        "metrics": [],
        "message": "success"
    }

    embedding_client = GeminiEmbeddingClient()
    query_embedding = await embedding_client.embed_text(query)
    
    hits = await search_semantic_index(query_embedding = query_embedding,tenant_id = tenant_id, object_types = ["metric"], top_k = top_k)
    if len(hits) == 0:
        result["message"] = "No matching metrics found."
    else:
        for hit in hits:
            if min_score is None or hit.similarity >= min_score:
                metric = {
                    "metric_name": hit.metadata["metric_name"],
                    "display_name": hit.metadata["display_name"],
                    "business_def": hit.metadata["business_def"],
                    "sql_expr": hit.metadata["sql_expr"],
                    "base_table": hit.metadata["base_table"],
                    "time_column": hit.metadata["time_column"],
                    "dimensions": hit.metadata["dimensions"],
                    "filters": hit.metadata["filters"],
                    "join_tables": hit.metadata["join_tables"],
                    "forbidden": hit.metadata["forbidden"],
                    "synonyms": hit.metadata["synonyms"],
                    "score": hit.similarity,
                }
                result["metrics"].append(metric)
    return result

async def search_schema(
    query: str,
    tenant_id: str,
    top_k: int = 8,
    object_types: tuple[str, ...] = ("table", "column"),
    table_names: list[str] | None = None,
    min_score: float | None = None,
) -> dict:
    if not query:
        raise ValueError("Tool[search_schema]: arg 'query' is empty")
    if not tenant_id:
        raise ValueError("Tool[search_schema]: arg 'tenant_id' is empty")
    if top_k < 1 or top_k >20:
        raise ValueError("Tool[search_schema]: arg 'top_k' limit at [1,20]")
    for object_type in object_types:
        if object_type not in ("table", "column"):
            raise ValueError("Tool[search_schema]: arg 'object_types' limit at (table, column)")

    embedding_client = GeminiEmbeddingClient()
    query_embedding = await embedding_client.embed_text(query)
    hits = await search_semantic_index(query_embedding = query_embedding,tenant_id = tenant_id, object_types = object_types, top_k = top_k)

    schema_by_table: dict[str, dict] = {}

    for hit in hits:
        metadata = hit.metadata
        table_name = metadata.get("table_name")

        if not table_name:
            continue
        if table_names and table_name not in table_names:
            continue
        if min_score and hit.similarity < min_score:
            continue

        
        table_entry = schema_by_table.setdefault(table_name,
        {
            "table_name": table_name,
            "table_comment": "",
            "score": hit.similarity,
            "columns": {},
        })

        table_entry["score"] = max(table_entry["score"], hit.similarity)

        if hit.object_type == "table":
            table_entry["table_comment"] = metadata.get("comment","")
        
        elif hit.object_type == "column":
            column_name = metadata.get("column_name")
            if not column_name:
                continue
            
            columns = table_entry["columns"]
        
            new_column = {
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
                columns[column_name] = new_column
            

    for table_name, table_entry in schema_by_table.items():
        cols = table_entry.get("columns")

        # 确保 cols 是字典且不为空
        if isinstance(cols, dict) and cols:
            table_entry["columns"] = sorted(
                cols.values(),
                key=lambda x: x.get("score", 0),
                reverse=True
            )
        else:
            # 如果没有列，设为空列表防止后续遍历报错
            table_entry["columns"] = []

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
