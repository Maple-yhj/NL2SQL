from __future__ import annotations

import argparse
import asyncio
from dataclasses import replace
from typing import Iterable

from catalog.loader import load_schema_catalog
from engine.metrics import MetricRegistry
from rag.documents import (
    EmbeddingDocument,
    build_metric_document,
    build_schema_documents,
)
from rag.embedding_client import GeminiEmbeddingClient
from rag.vector_store import upsert_documents


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Rebuild semantic_index embeddings.")
    # TODO: --tenant-id, --catalog-path, --scope, --batch-size, --dry-run, --dsn
    parser.add_argument('--tenant-id', default="demo")
    parser.add_argument('--catalog-path', default="schema_catalog.json")
    parser.add_argument('--scope', choices=["metrics","schema","all"], default= "all")
    parser.add_argument('--batch-size', type = int, default = 8)
    parser.add_argument('--dry-run', action = "store_true")
    parser.add_argument('--dsn', default=None)
    return parser.parse_args()


def chunked(items: list[EmbeddingDocument], size: int) -> Iterable[list[EmbeddingDocument]]:
    # TODO: yield items in fixed-size batches

    if(size <= 0):
        raise ValueError("batch size must be greater than 0")

    for start in range(0, len(items), size):
        yield items[start:start + size]


def force_tenant(doc: EmbeddingDocument, tenant_id: str) -> EmbeddingDocument:
    # 因为 EmbeddingDocument 是 frozen dataclass，用 replace() 覆盖 tenant_id
    return replace(doc, tenant_id=tenant_id)


def build_documents(args: argparse.Namespace) -> list[EmbeddingDocument]:
    docs: list[EmbeddingDocument] = []

    if args.scope in {"metrics", "all"}:
        # TODO:
        # 1. registry = MetricRegistry.default()
        # 2. metrics = registry.select([])
        # 3. build_metric_document(metric)
        # 4. force tenant_id if needed
        registry = MetricRegistry.default()
        metrics = registry.select([])
        for metric in metrics:
            docs.append(force_tenant(build_metric_document(metric),args.tenant_id))
        

    if args.scope in {"schema", "all"}:
        # TODO:
        # 1. catalog = load_schema_catalog(args.catalog_path)
        # 2. schema_docs = build_schema_documents(catalog, tenant_id=args.tenant_id)
        # 3. extend docs
        catalog = load_schema_catalog(args.catalog_path)
        docs.extend(build_schema_documents(catalog,tenant_id=args.tenant_id))
    
    if not docs:
        raise ValueError(f"No embedding documents built for scope={args.scope!r}")

    return docs


async def rebuild(args: argparse.Namespace) -> int:
    docs = build_documents(args)

    print(f"documents: {len(docs)}")
    print_document_summary(docs)

    if args.dry_run:
        return 0

    client = GeminiEmbeddingClient()
    total = 0

    for batch in chunked(docs, args.batch_size):
        # TODO:
        # 1. texts = [doc.content for doc in batch]
        # 2. embeddings = await client.embed_texts(texts)
        # 3. count = await upsert_documents(...)
        # 4. total += count
        # 5. print progress
        texts = [doc.content for doc in batch]
        embeddings = await client.embed_texts(texts)
        count = await upsert_documents(
            batch,
            embeddings,
            embedding_model= client.config.model,
            embedding_dim = client.config.dimension, 
            dsn = args.dsn
            )
        total+=count
    return total


def print_document_summary(docs: list[EmbeddingDocument]) -> None:
    # TODO: count by object_type and print first few object_key values
    metrics = 0
    tables = 0
    columns = 0

    for doc in docs:
        
        if doc.object_type == "metric":
            metrics+=1
        elif doc.object_type == "table":
            tables+=1
        elif doc.object_type == "column":
            columns+=1
        else:
            raise TypeError(f"embedding documents built Over type")

    print(f"metric:{metrics}, table:{tables}, column:{columns}")
    


def main() -> None:
    args = parse_args()
    total = asyncio.run(rebuild(args))
    print(f"upserted: {total}")


if __name__ == "__main__":
    main()
