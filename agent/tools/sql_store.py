from rag.embedding_client import GeminiEmbeddingClient
from rag.vector_store import SearchHit
from rag.vector_store import search_semantic_index
from typing import Any



async def search_metrics(
    query: str,
    tenant_id: str,
    top_k: int = 3,
    min_score: float | None = None,
) -> dict:
    '''
        
    
    '''
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
            if min_score and hit.similarity >= min_score:
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