"""JWT authentication helpers retained by the product API."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pwdlib import PasswordHash

from data_agent.runtime.environment import load_project_environment


@dataclass(frozen=True)
class AuthSettings:
    secret_key: str
    algorithm: str = "HS256"
    access_token_expire_minutes: int = 30
    refresh_token_expire_days: int = 7
    issuer: str = "nl2sql-api"
    audience: str = "nl2sql-client"


@dataclass(frozen=True)
class AuthPrincipal:
    tenant_id: str
    user_id: str
    username: str
    roles: list[str]
    token_version: int
    token_id: str


_password_hash = PasswordHash.recommended()
_bearer_scheme = HTTPBearer(auto_error=False)
_REQUIRED_TOKEN_CLAIMS = ["typ", "sub", "tenant_id", "ver", "iss", "aud", "iat", "exp", "jti"]


def load_auth_settings() -> AuthSettings:
    load_project_environment()

    secret_key = os.getenv("JWT_SECRET_KEY")
    if not secret_key:
        raise RuntimeError("JWT_SECRET_KEY is required")

    return AuthSettings(
        secret_key=secret_key,
        algorithm=os.getenv("JWT_ALGORITHM", "HS256"),
        access_token_expire_minutes=int(
            os.getenv("JWT_ACCESS_TOKEN_EXPIRE_MINUTES", "30")
        ),
        refresh_token_expire_days=int(os.getenv("JWT_REFRESH_TOKEN_EXPIRE_DAYS", "7")),
        issuer=os.getenv("JWT_ISSUER", "nl2sql-api"),
        audience=os.getenv("JWT_AUDIENCE", "nl2sql-client"),
    )


def hash_password(password: str) -> str:
    return _password_hash.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return _password_hash.verify(password, password_hash)
    except Exception:
        return False


def create_access_token(
    principal: AuthPrincipal, settings: AuthSettings | None = None
) -> str:
    settings = settings or load_auth_settings()
    now = datetime.now(UTC)
    expires_at = now + timedelta(minutes=settings.access_token_expire_minutes)
    payload = {
        "typ": "access",
        "sub": principal.user_id,
        "tenant_id": principal.tenant_id,
        "username": principal.username,
        "roles": principal.roles,
        "ver": principal.token_version,
        "iss": settings.issuer,
        "aud": settings.audience,
        "iat": now,
        "exp": expires_at,
        "jti": principal.token_id,
    }
    return jwt.encode(payload, settings.secret_key, algorithm=settings.algorithm)


def create_refresh_token(
    principal: AuthPrincipal, settings: AuthSettings | None = None
) -> tuple[str, str, datetime]:
    settings = settings or load_auth_settings()
    now = datetime.now(UTC)
    expires_at = now + timedelta(days=settings.refresh_token_expire_days)
    token_id = str(uuid4())
    payload = {
        "typ": "refresh",
        "sub": principal.user_id,
        "tenant_id": principal.tenant_id,
        "ver": principal.token_version,
        "iss": settings.issuer,
        "aud": settings.audience,
        "iat": now,
        "exp": expires_at,
        "jti": token_id,
    }
    token = jwt.encode(payload, settings.secret_key, algorithm=settings.algorithm)
    return token_id, token, expires_at


def _is_nonblank_string(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _is_numeric_date(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _validate_principal_claims(payload: dict, expected_type: str) -> None:
    if payload.get("typ") != expected_type:
        raise jwt.InvalidTokenError("Unexpected token type")

    if not _is_numeric_date(payload.get("iat")):
        raise jwt.InvalidTokenError("Invalid issued-at time")
    if not _is_numeric_date(payload.get("exp")):
        raise jwt.InvalidTokenError("Invalid expiration time")

    if not _is_nonblank_string(payload.get("sub")):
        raise jwt.InvalidTokenError("Invalid subject")
    if not _is_nonblank_string(payload.get("tenant_id")):
        raise jwt.InvalidTokenError("Invalid tenant")
    if not _is_nonblank_string(payload.get("jti")):
        raise jwt.InvalidTokenError("Invalid token id")

    token_version = payload.get("ver")
    if not isinstance(token_version, int) or isinstance(token_version, bool):
        raise jwt.InvalidTokenError("Invalid token version")

    if expected_type == "access":
        if not isinstance(payload.get("username"), str):
            raise jwt.InvalidTokenError("Invalid username")

        roles = payload.get("roles")
        if not isinstance(roles, list) or not all(
            isinstance(role, str) for role in roles
        ):
            raise jwt.InvalidTokenError("Invalid roles")


def decode_token(
    token: str, expected_type: str, settings: AuthSettings | None = None
) -> AuthPrincipal:
    settings = settings or load_auth_settings()
    try:
        payload = jwt.decode(
            token,
            settings.secret_key,
            algorithms=[settings.algorithm],
            issuer=settings.issuer,
            audience=settings.audience,
            options={"require": _REQUIRED_TOKEN_CLAIMS},
        )
        _validate_principal_claims(payload, expected_type)

        return AuthPrincipal(
            tenant_id=payload["tenant_id"],
            user_id=payload["sub"],
            username=payload.get("username", ""),
            roles=payload.get("roles", []),
            token_version=payload["ver"],
            token_id=payload["jti"],
        )
    except (KeyError, TypeError, ValueError, jwt.PyJWTError) as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication credentials",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc


def get_bearer_principal(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
) -> AuthPrincipal:
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return decode_token(credentials.credentials, "access")
