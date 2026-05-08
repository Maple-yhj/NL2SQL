from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Iterable, Sequence

from core.google_client import load_env_file
from rag.documents import EmbeddingDocument

DEFAULT_EMBEDDING_DIM = 768
VALID_OBJECT_TYPES = {"metric", "table", "column", "example_query"}

def _decode_metadata(value):
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        return json.loads(value)
    return {}


@dataclass(frozen=True, slots=True)
class SearchHit:
    id: int
    tenant_id: str
    object_type: str
    object_key: str
    content: str
    metadata: dict
    distance: float
    similarity: float


async def connect_vector_store(dsn: str | None = None):
    """Open a PostgreSQL connection and register pgvector codecs."""
    load_env_file()
    if dsn is None:
        dsn = os.getenv("DATABASE_URL") or os.getenv("POSTGRES_DSN")
    if not dsn:
        raise ValueError("Missing DATABASE_URL or POSTGRES_DSN.")

    import asyncpg
    from pgvector.asyncpg import register_vector

    conn = await asyncpg.connect(dsn)
    await register_vector(conn)
    return conn


def validate_embedding(embedding: Sequence[float], dim: int = DEFAULT_EMBEDDING_DIM) -> None:
    # check length and each item is int/float
    if len(embedding) != dim:
        raise ValueError(f"embedding's length != {dim}")


async def upsert_documents(
    docs: Sequence[EmbeddingDocument],
    embeddings: Sequence[Sequence[float]],
    *,
    embedding_model: str,
    embedding_dim: int = DEFAULT_EMBEDDING_DIM,
    dsn: str | None = None,
) -> int | None:
    """Insert or update semantic_index rows. Return affected document count."""
    # 1. len(docs) must equal len(embeddings)
    # 2. validate each embedding
    # 3. connect
    # 4. use transaction
    # 5. executemany INSERT ... ON CONFLICT ... DO UPDATE
    # 6. close connection in finally
    if len(docs) != len(embeddings):
        raise ValueError(f"EmbeddingDocument's length is {len(docs)} and embeddings' length is {len(embeddings)}")
    
    conn = await connect_vector_store()
    
    try:
        async with conn.transaction():
            for doc, embedding in zip(docs, embeddings):
                await conn.execute(
                    """
                    INSERT INTO semantic_index (
                        tenant_id,
                        object_type,
                        object_key,
                        source_table,
                        source_id,
                        content,
                        metadata,
                        embedding,
                        embedding_model,
                        embedding_dim,
                        content_hash,
                        is_active
                    )
                    VALUES (
                        $1, $2, $3, $4, $5,
                        $6, $7::jsonb, $8,
                        $9, $10, $11, true
                    )
                    ON CONFLICT (tenant_id, object_type, object_key)
                    DO UPDATE SET
                        content = EXCLUDED.content,
                        metadata = EXCLUDED.metadata,
                        embedding = EXCLUDED.embedding,
                        embedding_model = EXCLUDED.embedding_model,
                        embedding_dim = EXCLUDED.embedding_dim,
                        content_hash = EXCLUDED.content_hash,
                        is_active = true,
                        updated_at = now()
                    """,
                    doc.tenant_id,
                    doc.object_type,
                    doc.object_key,
                    doc.source_table,
                    doc.source_id,
                    doc.content,
                    json.dumps(doc.metadata, ensure_ascii=False),
                    embedding,
                    embedding_model,
                    len(embedding),
                    doc.content_hash(),
                )
    finally:
        await conn.close()
    


    


async def search_semantic_index(
    query_embedding: Sequence[float],
    *,
    tenant_id: str = "demo",
    object_types: Iterable[str] | None = None,
    top_k: int = 5,
    dsn: str | None = None,
) -> list[SearchHit]:
    """Return nearest semantic_index documents for one query embedding."""
    # 1. validate query embedding
    # 2. validate object_types against VALID_OBJECT_TYPES
    # 3. clamp top_k, for example 1..20
    # 4. SELECT rows WHERE tenant_id=$1 AND is_active=true
    # 5. optional object_type filter
    # 6. ORDER BY embedding <=> $query_embedding
    # 7. convert rows to SearchHit

    hits : list[SearchHit] = []
    validate_embedding(query_embedding)

    selected_types = list(object_types or [])
    invalid_types = set(selected_types) - VALID_OBJECT_TYPES
    if invalid_types:
        raise ValueError(f"Invalid object_types: {sorted(invalid_types)}")

    top_k = max(1, min(top_k,20))


    conn = await connect_vector_store(dsn)

    try:
        rows = await conn.fetch(
                """
                SELECT
                    id,
                    tenant_id,
                    object_type,
                    object_key,
                    content,
                    metadata,
                    embedding <=> $1 AS distance
                FROM semantic_index
                WHERE tenant_id = $2
                  AND is_active = true
                  AND (
                        $3::text[] IS NULL
                        OR object_type = ANY($3::text[])
                      )
                ORDER BY embedding <=> $1
                LIMIT $4
                """,
                query_embedding,
                tenant_id,
                selected_types or None,
                top_k,
            )
        for row in rows:
            distance = float(row["distance"])
            hits.append(
                SearchHit(
                    id=row["id"],
                    tenant_id=row["tenant_id"],
                    object_type=row["object_type"],
                    object_key=row["object_key"],
                    content=row["content"],
                    metadata=_decode_metadata(row["metadata"]),
                    distance=distance,
                    similarity=1 - distance,
                )
            )
        return hits
    finally:
        conn.close()