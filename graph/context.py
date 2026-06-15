from __future__ import annotations

from dataclasses import dataclass

from core.embeddings import EmbeddingClientProtocol
from core.llm import LLMProtocol


@dataclass(frozen=True, slots=True)
class GraphContext:
    llm: LLMProtocol
    embeddings: EmbeddingClientProtocol
    dsn: str | None = None
    timeout_ms: int = 10_000
    max_limit: int = 1000
    max_validation_attempts: int = 2
