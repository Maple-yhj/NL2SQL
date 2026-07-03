from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from pydantic.json_schema import SkipJsonSchema

from core.embeddings import EmbeddingClientProtocol, create_embedding_client
from core.llm import LLMProtocol, create_llm
from graph.data_memory import DataMemoryStoreProtocol, create_data_memory_store
from graph.memory_store import ConversationStoreProtocol, create_conversation_store


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
    memory_store: SkipJsonSchema[ConversationStoreProtocol] = Field(
        default_factory=lambda: create_conversation_store(),
        exclude=True,
        repr=False,
    )
    data_memory_store: SkipJsonSchema[DataMemoryStoreProtocol] = Field(
        default_factory=lambda: create_data_memory_store(),
        exclude=True,
        repr=False,
    )
    dsn: str | None = None
    memory_dsn: str | None = None
    data_memory_provider: str = ""
    data_memory_recall_limit: int = 5
    timeout_ms: int = 10_000
    max_limit: int = 1000
    max_validation_attempts: int = 2
    memory_history_limit: int = 8

    def model_post_init(self, __context: Any) -> None:
        if self.memory_dsn and "memory_store" not in self.__pydantic_fields_set__:
            object.__setattr__(
                self,
                "memory_store",
                create_conversation_store(self.memory_dsn),
            )
