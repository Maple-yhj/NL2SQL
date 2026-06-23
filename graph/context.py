from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field
from pydantic.json_schema import SkipJsonSchema

from core.embeddings import EmbeddingClientProtocol, create_embedding_client
from core.llm import LLMProtocol, create_llm


class GraphContext(BaseModel):
    model_config = ConfigDict(
        arbitrary_types_allowed=True,
        extra="ignore",
        frozen=True,
    )

    llm: SkipJsonSchema[LLMProtocol] = Field(
        default_factory=lambda: create_llm(),
        exclude=True,
        repr=False,
    )
    embeddings: SkipJsonSchema[EmbeddingClientProtocol] = Field(
        default_factory=lambda: create_embedding_client(),
        exclude=True,
        repr=False,
    )
    dsn: str | None = None
    timeout_ms: int = 10_000
    max_limit: int = 1000
    max_validation_attempts: int = 2
