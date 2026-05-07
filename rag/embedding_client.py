from __future__ import annotations

import os
from dataclasses import dataclass

from core.google_client import get_client, load_env_file
from google.genai import types


DEFAULT_EMBEDDING_MODEL = "gemini-embedding-001"
DEFAULT_EMBEDDING_DIM = 768


@dataclass(frozen=True, slots=True)
class EmbeddingConfig:
    model: str = DEFAULT_EMBEDDING_MODEL
    dimension: int = DEFAULT_EMBEDDING_DIM


def get_embedding_config() -> EmbeddingConfig:
    load_env_file()
    return EmbeddingConfig(
        model=os.getenv("EMBEDDING_MODEL", DEFAULT_EMBEDDING_MODEL),
        dimension=int(os.getenv("EMBEDDING_DIM", str(DEFAULT_EMBEDDING_DIM))),
    )


def _validate_vectors(
    vectors: list[list[float]],
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


class GeminiEmbeddingClient:
    def __init__(self, client=None, config: EmbeddingConfig | None = None):
        self.client = client or get_client()
        self.config = config or get_embedding_config()

    async def embed_text(self, text: str) -> list[float]:
        vectors = await self.embed_texts([text])
        return vectors[0]

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        # 1. 校验 texts 非空
        if not texts:
            print("[WARN] embed_texts fail")
            return list[list[float]]()
        # 2. 调 Gemini embed_content
        result = await self.client.aio.models.embed_content(
            model=self.config.model,
            contents=texts,
            config=types.EmbedContentConfig(output_dimensionality=self.config.dimension),
        )
        # 3. 从 response.embeddings 里取 values
        vectors = []
        if result.embeddings:
            for embedding in result.embeddings:
                if embedding.values:
                    vectors.append([float(value) for value in embedding.values])
        # 4. 校验数量和维度
        _validate_vectors(vectors = vectors,expected_count = len(texts), expected_dim =self.config.dimension)
        # 5. 返回 list[list[float]]
        return vectors