"""Strict HTTP schemas for auth and Runtime-backed product routes."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from data_agent.runtime.models import (
    AgentRequest,
    AgentResponse,
    ConversationMessage,
    ConversationSummary,
)


def _strip_required_text(value: str) -> str:
    stripped = value.strip()
    if not stripped:
        raise ValueError("must not be blank")
    return stripped


class StrictApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LoginRequest(StrictApiModel):
    tenant_id: str = Field(default="demo", min_length=1)
    username: str = Field(min_length=1)
    password: str = Field(min_length=1)

    @field_validator("tenant_id", "username", "password")
    @classmethod
    def strip_required_text(cls, value: str) -> str:
        return _strip_required_text(value)


class RefreshRequest(StrictApiModel):
    refresh_token: str = Field(min_length=1)

    @field_validator("refresh_token")
    @classmethod
    def strip_refresh_token(cls, value: str) -> str:
        return _strip_required_text(value)


class LogoutRequest(StrictApiModel):
    refresh_token: str = ""

    @field_validator("refresh_token")
    @classmethod
    def strip_optional_refresh_token(cls, value: str) -> str:
        return value.strip()


class AuthUserResponse(StrictApiModel):
    tenant_id: str
    user_id: str
    username: str
    roles: list[str]


class TokenResponse(StrictApiModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int
    user: AuthUserResponse


class AccessTokenResponse(StrictApiModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int


class LogoutResponse(StrictApiModel):
    ok: bool = True


class Nl2SqlRequest(AgentRequest):
    """The HTTP agent request is exactly the public Runtime request."""


Nl2SqlResponse = AgentResponse


class ConversationCreateRequest(StrictApiModel):
    title: str = ""
    domain_id: str = Field(default="commerce", min_length=1)

    @field_validator("title")
    @classmethod
    def strip_title(cls, value: str) -> str:
        return value.strip()

    @field_validator("domain_id")
    @classmethod
    def strip_domain_id(cls, value: str) -> str:
        return _strip_required_text(value)


class ConversationUpdateRequest(StrictApiModel):
    title: str | None = None
    archived: bool | None = None

    @field_validator("title")
    @classmethod
    def strip_optional_title(cls, value: str | None) -> str | None:
        return value.strip() if value is not None else None


class ConversationMessageRequest(AgentRequest):
    """Conversation turns use the same strict Runtime contract."""


ConversationResponse = ConversationSummary


class ConversationListResponse(StrictApiModel):
    items: list[ConversationSummary]


class ConversationMessagesResponse(StrictApiModel):
    items: list[ConversationMessage]


ConversationNl2SqlResponse = AgentResponse


__all__ = [
    "AccessTokenResponse",
    "AuthUserResponse",
    "ConversationCreateRequest",
    "ConversationListResponse",
    "ConversationMessage",
    "ConversationMessageRequest",
    "ConversationMessagesResponse",
    "ConversationNl2SqlResponse",
    "ConversationResponse",
    "ConversationUpdateRequest",
    "LoginRequest",
    "LogoutRequest",
    "LogoutResponse",
    "Nl2SqlRequest",
    "Nl2SqlResponse",
    "RefreshRequest",
    "TokenResponse",
]
