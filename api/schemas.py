from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


def _strip_required_text(value: str) -> str:
    stripped = value.strip()
    if not stripped:
        raise ValueError("must not be blank")
    return stripped


def _strip_optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    if not stripped:
        raise ValueError("must not be blank")
    return stripped


class LoginRequest(BaseModel):
    tenant_id: str = Field(default="demo", min_length=1)
    username: str = Field(min_length=1)
    password: str = Field(min_length=1)

    @field_validator("tenant_id", "username", "password")
    @classmethod
    def strip_required_text(cls, value: str) -> str:
        return _strip_required_text(value)


class RefreshRequest(BaseModel):
    refresh_token: str = Field(min_length=1)

    @field_validator("refresh_token")
    @classmethod
    def strip_refresh_token(cls, value: str) -> str:
        return _strip_required_text(value)


class LogoutRequest(BaseModel):
    refresh_token: str = ""

    @field_validator("refresh_token")
    @classmethod
    def strip_optional_refresh_token(cls, value: str) -> str:
        return value.strip()


class AuthUserResponse(BaseModel):
    tenant_id: str
    user_id: str
    username: str
    roles: list[str]


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int
    user: AuthUserResponse


class AccessTokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int


class LogoutResponse(BaseModel):
    ok: bool = True


class Nl2SqlRequest(BaseModel):
    question: str = Field(min_length=1)
    tenant_id: str | None = Field(default=None, min_length=1)
    execute: bool = True
    timeout_ms: int = Field(default=10_000, ge=1_000, le=60_000)
    max_limit: int = Field(default=1_000, ge=1, le=10_000)
    max_validation_attempts: int = Field(default=2, ge=1, le=5)
    agent_mode: Literal["fixed", "dynamic"] = "dynamic"
    include_tool_trace: bool = False

    @field_validator("question")
    @classmethod
    def strip_required_text(cls, value: str) -> str:
        return _strip_required_text(value)

    @field_validator("tenant_id")
    @classmethod
    def strip_optional_text(cls, value: str | None) -> str | None:
        return _strip_optional_text(value)


class Nl2SqlResponse(BaseModel):
    ok: bool
    question: str
    tenant_id: str
    intent: dict[str, Any]
    sql: str
    message_type: Literal["text", "table", "error"]
    rows: list[dict[str, Any]]
    answer: str
    error: str
    trace: list[dict[str, Any]]
    tool_trace: list[dict[str, Any]] | None = None
    pending_memory_updates: list[dict[str, Any]] = Field(default_factory=list)


class ConversationCreateRequest(BaseModel):
    tenant_id: str | None = Field(default=None, min_length=1)
    user_id: str | None = Field(default=None, min_length=1)
    title: str = ""

    @field_validator("tenant_id", "user_id")
    @classmethod
    def strip_optional_text(cls, value: str | None) -> str | None:
        return _strip_optional_text(value)

    @field_validator("title")
    @classmethod
    def strip_title(cls, value: str) -> str:
        return value.strip()


class ConversationUpdateRequest(BaseModel):
    tenant_id: str | None = Field(default=None, min_length=1)
    user_id: str | None = Field(default=None, min_length=1)
    title: str | None = None
    archived: bool | None = None

    @field_validator("tenant_id", "user_id")
    @classmethod
    def strip_optional_identity_text(cls, value: str | None) -> str | None:
        return _strip_optional_text(value)

    @field_validator("title")
    @classmethod
    def strip_optional_title(cls, value: str | None) -> str | None:
        return value.strip() if value is not None else None


class ConversationMessageRequest(BaseModel):
    question: str = Field(min_length=1)
    tenant_id: str | None = Field(default=None, min_length=1)
    user_id: str | None = Field(default=None, min_length=1)
    execute: bool = True
    timeout_ms: int = Field(default=10_000, ge=1_000, le=60_000)
    max_limit: int = Field(default=1_000, ge=1, le=10_000)
    max_validation_attempts: int = Field(default=2, ge=1, le=5)
    memory_history_limit: int = Field(default=8, ge=0, le=50)
    agent_mode: Literal["fixed", "dynamic"] = "dynamic"
    include_tool_trace: bool = False

    @field_validator("question")
    @classmethod
    def strip_required_text(cls, value: str) -> str:
        return _strip_required_text(value)

    @field_validator("tenant_id", "user_id")
    @classmethod
    def strip_optional_identity_text(cls, value: str | None) -> str | None:
        return _strip_optional_text(value)


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
    message_type: Literal["text", "table", "error"]
    rows: list[dict[str, Any]]
    answer: str
    error: str
    trace: list[dict[str, Any]]
    tool_trace: list[dict[str, Any]] | None = None
    pending_memory_updates: list[dict[str, Any]] = Field(default_factory=list)


class ErrorResponse(BaseModel):
    ok: bool = False
    error: str
    detail: str = ""
