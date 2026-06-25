from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator


class Nl2SqlRequest(BaseModel):
    question: str = Field(min_length=1)
    tenant_id: str = Field(default="demo", min_length=1)
    execute: bool = False
    timeout_ms: int = Field(default=10_000, ge=1_000, le=60_000)
    max_limit: int = Field(default=1_000, ge=1, le=10_000)
    max_validation_attempts: int = Field(default=2, ge=1, le=5)

    @field_validator("question", "tenant_id")
    @classmethod
    def strip_required_text(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be blank")
        return stripped


class Nl2SqlResponse(BaseModel):
    ok: bool
    question: str
    tenant_id: str
    intent: dict[str, Any]
    sql: str
    rows: list[dict[str, Any]]
    answer: str
    error: str
    trace: list[dict[str, Any]]


class ConversationCreateRequest(BaseModel):
    tenant_id: str = Field(default="demo", min_length=1)
    user_id: str = Field(min_length=1)
    title: str = ""

    @field_validator("tenant_id", "user_id", "title")
    @classmethod
    def strip_text(cls, value: str) -> str:
        return value.strip()


class ConversationUpdateRequest(BaseModel):
    tenant_id: str = Field(default="demo", min_length=1)
    user_id: str = Field(min_length=1)
    title: str | None = None
    archived: bool | None = None

    @field_validator("tenant_id", "user_id")
    @classmethod
    def strip_required_text(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be blank")
        return stripped

    @field_validator("title")
    @classmethod
    def strip_optional_text(cls, value: str | None) -> str | None:
        return value.strip() if value is not None else None


class ConversationMessageRequest(BaseModel):
    question: str = Field(min_length=1)
    tenant_id: str = Field(default="demo", min_length=1)
    user_id: str = Field(min_length=1)
    execute: bool = False
    timeout_ms: int = Field(default=10_000, ge=1_000, le=60_000)
    max_limit: int = Field(default=1_000, ge=1, le=10_000)
    max_validation_attempts: int = Field(default=2, ge=1, le=5)
    memory_history_limit: int = Field(default=8, ge=0, le=50)

    @field_validator("question", "tenant_id", "user_id")
    @classmethod
    def strip_required_text(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be blank")
        return stripped


class ConversationResponse(BaseModel):
    tenant_id: str
    conversation_id: str
    user_id: str
    title: str
    archived: bool
    created_at: str
    updated_at: str


class ConversationListResponse(BaseModel):
    items: list[ConversationResponse]


class ConversationMessage(BaseModel):
    role: str
    content: str
    metadata: dict[str, Any]


class ConversationMessagesResponse(BaseModel):
    items: list[ConversationMessage]


class ConversationNl2SqlResponse(BaseModel):
    ok: bool
    question: str
    contextualized_question: str
    conversation_id: str
    user_id: str
    tenant_id: str
    intent: dict[str, Any]
    sql: str
    rows: list[dict[str, Any]]
    answer: str
    error: str
    trace: list[dict[str, Any]]


class ErrorResponse(BaseModel):
    ok: bool = False
    error: str
    detail: str = ""
