from __future__ import annotations

import os
from typing import Protocol, runtime_checkable

from dotenv import load_dotenv
from langchain_google_genai import GoogleGenerativeAIEmbeddings


DEFAULT_EMBEDDING_MODEL = "models/gemini-embedding-001"
DEFAULT_EMBEDDING_DIM = 768


@runtime_checkable
class EmbeddingClientProtocol(Protocol):
    model_name: str
    dimension: int

    async def embed_text(self, text: str) -> list[float]:
        ...

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        ...


def _validate_vectors(
    vectors: list[list[float]],
    *,
    expected_count: int,
    expected_dim: int,
) -> None:
    if len(vectors) != expected_count:
        raise ValueError(f"Expected {expected_count} embeddings, got {len(vectors)}")
    for index, vector in enumerate(vectors):
        if len(vector) != expected_dim:
            raise ValueError(
                f"Expected embedding dim {expected_dim} at index {index}, got {len(vector)}"
            )


class LangChainEmbeddingClient:
    def __init__(self, embeddings, *, model_name: str, dimension: int) -> None:
        self.embeddings = embeddings
        self.model_name = model_name
        self.dimension = dimension

    async def embed_text(self, text: str) -> list[float]:
        vector = [float(value) for value in await self.embeddings.aembed_query(text)]
        _validate_vectors([vector], expected_count=1, expected_dim=self.dimension)
        return vector

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        vectors = [
            [float(value) for value in vector]
            for vector in await self.embeddings.aembed_documents(texts)
        ]
        _validate_vectors(
            vectors,
            expected_count=len(texts),
            expected_dim=self.dimension,
        )
        return vectors


def create_embedding_client() -> EmbeddingClientProtocol:
    load_dotenv()
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise ValueError("Missing GEMINI_API_KEY for embeddings.")
    model_name = os.getenv("EMBEDDING_MODEL", DEFAULT_EMBEDDING_MODEL)
    dimension = int(os.getenv("EMBEDDING_DIM", str(DEFAULT_EMBEDDING_DIM)))
    embeddings = GoogleGenerativeAIEmbeddings(
        model=model_name,
        api_key=api_key,
        output_dimensionality=dimension,
    )
    return LangChainEmbeddingClient(
        embeddings,
        model_name=model_name,
        dimension=dimension,
    )
