# Database Auth JWT Implementation Plan

> **Historical / retained subsystem:** Authentication and principal authority remain current. Agent execution and datasource authority are now defined by the 2026-08-08 design.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add database-backed API users, JWT access tokens, rotating refresh tokens, and authenticated identity enforcement for the current FastAPI API.

**Architecture:** Keep auth isolated in `api.auth` and `api.auth_store`, then wire it into `api.routes` through FastAPI dependencies. Store users and refresh-token sessions in the service metadata database, while existing NL2SQL and conversation code consumes trusted `tenant_id/user_id` values derived from the token.

**Tech Stack:** FastAPI, Pydantic, PyJWT, pwdlib Argon2 password hashing, asyncpg, unittest, FastAPI TestClient.

---

## File Structure

- Create `api/auth.py`: auth settings, password hashing, JWT creation/decoding, principal model, and FastAPI dependencies.
- Create `api/auth_store.py`: auth user/refresh-token protocols, in-memory test store, Postgres store, token hashing helpers, and store factory.
- Modify `api/schemas.py`: auth request/response models and optional identity fields for protected request models.
- Modify `api/routes.py`: auth endpoints and authenticated identity enforcement for existing routes.
- Create `db/auth.sql`: users and refresh-token session tables.
- Create `scripts/create_auth_user.py`: operator script for provisioning users.
- Modify `pyproject.toml`: add `PyJWT` and `pwdlib[argon2]`.
- Modify `.env.example`: document auth configuration.
- Modify `README.md`: document setup, user provisioning, login, refresh, logout, and authenticated API calls.
- Create `tests/test_api_auth.py`: route-level auth tests.
- Create `tests/test_auth_tokens.py`: password/JWT unit tests.
- Create `tests/test_auth_store.py`: store/session lifecycle tests.
- Modify `tests/test_api_nl2sql.py` and `tests/test_api_conversations.py`: send access tokens and assert identity enforcement.

## Task 1: Auth Database Schema

**Files:**
- Create: `db/auth.sql`

- [ ] **Step 1: Add the schema file**

Create `db/auth.sql` with this content:

```sql
CREATE TABLE IF NOT EXISTS auth_users (
    id BIGSERIAL PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    username TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    roles JSONB NOT NULL DEFAULT '["user"]'::jsonb,
    disabled BOOLEAN NOT NULL DEFAULT false,
    token_version INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_login_at TIMESTAMPTZ,
    UNIQUE (tenant_id, user_id),
    UNIQUE (tenant_id, username)
);

CREATE INDEX IF NOT EXISTS idx_auth_users_login
    ON auth_users (tenant_id, username)
    WHERE disabled = false;

CREATE TABLE IF NOT EXISTS auth_refresh_tokens (
    id BIGSERIAL PRIMARY KEY,
    token_id TEXT NOT NULL UNIQUE,
    token_hash TEXT NOT NULL UNIQUE,
    tenant_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    revoked_at TIMESTAMPTZ,
    replaced_by_token_id TEXT,
    user_agent TEXT,
    client_ip TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_used_at TIMESTAMPTZ,
    FOREIGN KEY (tenant_id, user_id)
        REFERENCES auth_users (tenant_id, user_id)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_auth_refresh_tokens_user
    ON auth_refresh_tokens (tenant_id, user_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_auth_refresh_tokens_active
    ON auth_refresh_tokens (token_id, expires_at)
    WHERE revoked_at IS NULL;
```

- [ ] **Step 2: Commit**

```bash
git add db/auth.sql
git commit -m "feat: add auth database schema"
```

## Task 2: Auth Schemas

**Files:**
- Modify: `api/schemas.py`
- Test: `tests/test_api_auth.py`

- [ ] **Step 1: Write failing schema tests**

Add these tests to a new `tests/test_api_auth.py`:

```python
import unittest

from pydantic import ValidationError

from api.schemas import LoginRequest, RefreshRequest


class ApiAuthSchemaTests(unittest.TestCase):
    def test_login_request_strips_text(self):
        request = LoginRequest(
            tenant_id=" demo ",
            username=" alice ",
            password=" secret ",
        )

        self.assertEqual(request.tenant_id, "demo")
        self.assertEqual(request.username, "alice")
        self.assertEqual(request.password, "secret")

    def test_login_request_rejects_blank_password(self):
        with self.assertRaises(ValidationError):
            LoginRequest(username="alice", password="   ")

    def test_refresh_request_rejects_blank_token(self):
        with self.assertRaises(ValidationError):
            RefreshRequest(refresh_token="   ")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify failure**

Run: `python -m unittest tests.test_api_auth.ApiAuthSchemaTests -v`

Expected: FAIL because `LoginRequest` and `RefreshRequest` do not exist.

- [ ] **Step 3: Add auth schemas and make identity request fields optional**

In `api/schemas.py`, add these models:

```python
class LoginRequest(BaseModel):
    tenant_id: str = Field(default="demo", min_length=1)
    username: str = Field(min_length=1)
    password: str = Field(min_length=1)

    @field_validator("tenant_id", "username", "password")
    @classmethod
    def strip_required_text(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be blank")
        return stripped


class RefreshRequest(BaseModel):
    refresh_token: str = Field(min_length=1)

    @field_validator("refresh_token")
    @classmethod
    def strip_refresh_token(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be blank")
        return stripped


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
```

Change protected request identity fields so token identity can be used when clients omit them:

```python
class ConversationCreateRequest(BaseModel):
    tenant_id: str | None = Field(default=None, min_length=1)
    user_id: str | None = Field(default=None, min_length=1)
    title: str = ""
```

Apply the same `str | None = None` pattern to `ConversationUpdateRequest.tenant_id`, `ConversationUpdateRequest.user_id`, `ConversationMessageRequest.tenant_id`, and `ConversationMessageRequest.user_id`. Keep `Nl2SqlRequest.tenant_id` as optional with default `None` so `/api/nl2sql` also derives tenant from the token.

- [ ] **Step 4: Run tests to verify pass**

Run: `python -m unittest tests.test_api_auth.ApiAuthSchemaTests -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add api/schemas.py tests/test_api_auth.py
git commit -m "feat: add auth api schemas"
```

## Task 3: Password And JWT Service

**Files:**
- Create: `api/auth.py`
- Test: `tests/test_auth_tokens.py`
- Modify: `pyproject.toml`

- [ ] **Step 1: Add dependencies**

In `pyproject.toml`, add:

```toml
  "PyJWT>=2.10",
  "pwdlib[argon2]>=0.2",
```

- [ ] **Step 2: Write failing token tests**

Create `tests/test_auth_tokens.py`:

```python
import unittest
from datetime import UTC, datetime, timedelta
from unittest import mock

from fastapi import HTTPException

from api.auth import (
    AuthPrincipal,
    AuthSettings,
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    verify_password,
)


class AuthTokenTests(unittest.TestCase):
    def settings(self) -> AuthSettings:
        return AuthSettings(
            secret_key="test-secret-key",
            algorithm="HS256",
            access_token_expire_minutes=30,
            refresh_token_expire_days=7,
            issuer="nl2sql-api-test",
            audience="nl2sql-client-test",
        )

    def principal(self) -> AuthPrincipal:
        return AuthPrincipal(
            tenant_id="demo",
            user_id="user-1",
            username="alice",
            roles=["user"],
            token_version=0,
            token_id="access-id",
        )

    def test_password_hash_verifies_password(self):
        password_hash = hash_password("correct horse battery staple")

        self.assertTrue(verify_password("correct horse battery staple", password_hash))
        self.assertFalse(verify_password("wrong", password_hash))

    def test_access_token_decodes_as_access_only(self):
        token = create_access_token(self.principal(), self.settings())

        decoded = decode_token(token, expected_type="access", settings=self.settings())

        self.assertEqual(decoded.tenant_id, "demo")
        self.assertEqual(decoded.user_id, "user-1")
        self.assertEqual(decoded.username, "alice")
        self.assertEqual(decoded.roles, ["user"])

    def test_refresh_token_rejected_when_decoded_as_access(self):
        token_id, token, _ = create_refresh_token(self.principal(), self.settings())

        self.assertTrue(token_id)
        with self.assertRaises(HTTPException) as raised:
            decode_token(token, expected_type="access", settings=self.settings())

        self.assertEqual(raised.exception.status_code, 401)

    def test_expired_token_returns_401(self):
        now = datetime.now(UTC)
        with mock.patch("api.auth._utc_now", return_value=now - timedelta(hours=1)):
            token = create_access_token(self.principal(), self.settings())

        with self.assertRaises(HTTPException) as raised:
            decode_token(token, expected_type="access", settings=self.settings())

        self.assertEqual(raised.exception.status_code, 401)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 3: Run tests to verify failure**

Run: `python -m unittest tests.test_auth_tokens -v`

Expected: FAIL because `api.auth` does not exist.

- [ ] **Step 4: Implement token service**

Create `api/auth.py` with:

```python
from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pwdlib import PasswordHash

from core.environment import load_project_environment


_password_hash = PasswordHash.recommended()
_bearer = HTTPBearer(auto_error=False)


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


def load_auth_settings() -> AuthSettings:
    load_project_environment()
    secret_key = os.getenv("JWT_SECRET_KEY", "").strip()
    if not secret_key:
        raise RuntimeError("JWT_SECRET_KEY is required")
    return AuthSettings(
        secret_key=secret_key,
        algorithm=os.getenv("JWT_ALGORITHM", "HS256").strip() or "HS256",
        access_token_expire_minutes=int(os.getenv("JWT_ACCESS_TOKEN_EXPIRE_MINUTES", "30")),
        refresh_token_expire_days=int(os.getenv("JWT_REFRESH_TOKEN_EXPIRE_DAYS", "7")),
        issuer=os.getenv("JWT_ISSUER", "nl2sql-api").strip() or "nl2sql-api",
        audience=os.getenv("JWT_AUDIENCE", "nl2sql-client").strip() or "nl2sql-client",
    )


def hash_password(password: str) -> str:
    return _password_hash.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return _password_hash.verify(password, password_hash)
    except Exception:
        return False


def create_access_token(principal: AuthPrincipal, settings: AuthSettings | None = None) -> str:
    resolved = settings or load_auth_settings()
    now = _utc_now()
    expires_at = now + timedelta(minutes=resolved.access_token_expire_minutes)
    payload = _base_payload(principal, "access", now, expires_at, str(uuid.uuid4()), resolved)
    payload["username"] = principal.username
    payload["roles"] = principal.roles
    return jwt.encode(payload, resolved.secret_key, algorithm=resolved.algorithm)


def create_refresh_token(
    principal: AuthPrincipal,
    settings: AuthSettings | None = None,
) -> tuple[str, str, datetime]:
    resolved = settings or load_auth_settings()
    now = _utc_now()
    expires_at = now + timedelta(days=resolved.refresh_token_expire_days)
    token_id = str(uuid.uuid4())
    payload = _base_payload(principal, "refresh", now, expires_at, token_id, resolved)
    token = jwt.encode(payload, resolved.secret_key, algorithm=resolved.algorithm)
    return token_id, token, expires_at


def decode_token(
    token: str,
    *,
    expected_type: str,
    settings: AuthSettings | None = None,
) -> AuthPrincipal:
    resolved = settings or load_auth_settings()
    credentials_error = _credentials_error()
    try:
        payload = jwt.decode(
            token,
            resolved.secret_key,
            algorithms=[resolved.algorithm],
            issuer=resolved.issuer,
            audience=resolved.audience,
        )
    except jwt.PyJWTError as exc:
        raise credentials_error from exc
    if payload.get("typ") != expected_type:
        raise credentials_error
    try:
        return AuthPrincipal(
            tenant_id=str(payload["tenant_id"]),
            user_id=str(payload["sub"]),
            username=str(payload.get("username", "")),
            roles=[str(role) for role in payload.get("roles", [])],
            token_version=int(payload.get("ver", 0)),
            token_id=str(payload["jti"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise credentials_error from exc


async def get_bearer_principal(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> AuthPrincipal:
    if credentials is None:
        raise _credentials_error()
    return decode_token(credentials.credentials, expected_type="access")


def _base_payload(
    principal: AuthPrincipal,
    token_type: str,
    issued_at: datetime,
    expires_at: datetime,
    token_id: str,
    settings: AuthSettings,
) -> dict[str, Any]:
    return {
        "typ": token_type,
        "sub": principal.user_id,
        "tenant_id": principal.tenant_id,
        "ver": principal.token_version,
        "iss": settings.issuer,
        "aud": settings.audience,
        "iat": issued_at,
        "exp": expires_at,
        "jti": token_id,
    }


def _credentials_error() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )


def _utc_now() -> datetime:
    return datetime.now(UTC)
```

- [ ] **Step 5: Run tests to verify pass**

Run: `python -m unittest tests.test_auth_tokens -v`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml api/auth.py tests/test_auth_tokens.py
git commit -m "feat: add jwt token service"
```

## Task 4: Auth Store And Refresh Token Lifecycle

**Files:**
- Create: `api/auth_store.py`
- Test: `tests/test_auth_store.py`

- [ ] **Step 1: Write failing store tests**

Create `tests/test_auth_store.py`:

```python
import unittest
from datetime import UTC, datetime, timedelta

from api.auth_store import InMemoryAuthStore, hash_refresh_token


class AuthStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_find_user_by_login_returns_active_user(self):
        store = InMemoryAuthStore()
        await store.upsert_user(
            tenant_id="demo",
            user_id="user-1",
            username="alice",
            password_hash="hash",
            roles=["user"],
            disabled=False,
        )

        user = await store.find_user_by_login(tenant_id="demo", username="alice")

        self.assertIsNotNone(user)
        self.assertEqual(user["user_id"], "user-1")
        self.assertEqual(user["roles"], ["user"])

    async def test_disabled_user_is_returned_for_login_policy(self):
        store = InMemoryAuthStore()
        await store.upsert_user(
            tenant_id="demo",
            user_id="user-1",
            username="alice",
            password_hash="hash",
            roles=["user"],
            disabled=True,
        )

        user = await store.find_user_by_login(tenant_id="demo", username="alice")

        self.assertTrue(user["disabled"])

    async def test_refresh_token_rotation_revokes_old_token(self):
        store = InMemoryAuthStore()
        expires_at = datetime.now(UTC) + timedelta(days=7)
        await store.store_refresh_token(
            token_id="old",
            token_hash=hash_refresh_token("old-token"),
            tenant_id="demo",
            user_id="user-1",
            expires_at=expires_at,
        )

        old = await store.get_active_refresh_token(
            token_id="old",
            token_hash=hash_refresh_token("old-token"),
        )
        self.assertIsNotNone(old)

        await store.rotate_refresh_token(
            old_token_id="old",
            new_token_id="new",
            new_token_hash=hash_refresh_token("new-token"),
            tenant_id="demo",
            user_id="user-1",
            expires_at=expires_at,
        )

        self.assertIsNone(
            await store.get_active_refresh_token(
                token_id="old",
                token_hash=hash_refresh_token("old-token"),
            )
        )
        self.assertIsNotNone(
            await store.get_active_refresh_token(
                token_id="new",
                token_hash=hash_refresh_token("new-token"),
            )
        )


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify failure**

Run: `python -m unittest tests.test_auth_store -v`

Expected: FAIL because `api.auth_store` does not exist.

- [ ] **Step 3: Implement auth store**

Create `api/auth_store.py` with protocol methods and both store implementations. Start with this public surface:

```python
from __future__ import annotations

import hashlib
import json
import os
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any, Protocol

import asyncpg

from core.environment import load_project_environment


AuthUser = dict[str, Any]
RefreshTokenRecord = dict[str, Any]


class AuthStoreProtocol(Protocol):
    async def find_user_by_login(self, *, tenant_id: str, username: str) -> AuthUser | None:
        ...

    async def get_user(self, *, tenant_id: str, user_id: str) -> AuthUser | None:
        ...

    async def upsert_user(
        self,
        *,
        tenant_id: str,
        user_id: str,
        username: str,
        password_hash: str,
        roles: list[str],
        disabled: bool = False,
    ) -> AuthUser:
        ...

    async def record_login(self, *, tenant_id: str, user_id: str) -> None:
        ...

    async def store_refresh_token(
        self,
        *,
        token_id: str,
        token_hash: str,
        tenant_id: str,
        user_id: str,
        expires_at: datetime,
        user_agent: str = "",
        client_ip: str = "",
    ) -> None:
        ...

    async def get_active_refresh_token(
        self,
        *,
        token_id: str,
        token_hash: str,
    ) -> RefreshTokenRecord | None:
        ...

    async def rotate_refresh_token(
        self,
        *,
        old_token_id: str,
        new_token_id: str,
        new_token_hash: str,
        tenant_id: str,
        user_id: str,
        expires_at: datetime,
        user_agent: str = "",
        client_ip: str = "",
    ) -> None:
        ...

    async def revoke_refresh_token(self, *, token_id: str, token_hash: str) -> None:
        ...


def hash_refresh_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
```

Implement `InMemoryAuthStore` using dictionaries keyed by `(tenant_id, username)`, `(tenant_id, user_id)`, and `token_id`. Implement `PostgresAuthStore` with SQL matching `db/auth.sql`. Implement:

```python
def create_auth_store(dsn: str | None = None) -> AuthStoreProtocol:
    load_project_environment()
    resolved = (
        dsn
        or os.getenv("AUTH_DATABASE_URL")
        or os.getenv("MEMORY_DATABASE_URL")
        or os.getenv("MEMORY_POSTGRES_DSN")
    )
    if not resolved:
        raise RuntimeError("Missing AUTH_DATABASE_URL or MEMORY_DATABASE_URL for auth.")
    return PostgresAuthStore(resolved)
```

- [ ] **Step 4: Run tests to verify pass**

Run: `python -m unittest tests.test_auth_store -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add api/auth_store.py tests/test_auth_store.py
git commit -m "feat: add auth store"
```

## Task 5: Auth Routes

**Files:**
- Modify: `api/routes.py`
- Modify: `tests/test_api_auth.py`

- [ ] **Step 1: Write failing route tests**

Append route tests to `tests/test_api_auth.py`:

```python
from fastapi.testclient import TestClient

from api.app import create_app
from api.auth import hash_password
from api.auth_store import InMemoryAuthStore


class ApiAuthRouteTests(unittest.TestCase):
    def setUp(self):
        self.store = InMemoryAuthStore()
        self.client = TestClient(create_app())

    def patch_auth(self):
        return mock.patch("api.routes.create_auth_store", return_value=self.store)

    def test_login_returns_token_pair(self):
        async def seed():
            await self.store.upsert_user(
                tenant_id="demo",
                user_id="user-1",
                username="alice",
                password_hash=hash_password("secret"),
                roles=["user"],
            )
        asyncio.run(seed())

        with self.patch_auth(), mock.patch.dict(
            "os.environ",
            {
                "JWT_SECRET_KEY": "test-secret-key",
                "JWT_ISSUER": "nl2sql-api",
                "JWT_AUDIENCE": "nl2sql-client",
            },
        ):
            response = self.client.post(
                "/api/auth/login",
                json={"tenant_id": "demo", "username": "alice", "password": "secret"},
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["token_type"], "bearer")
        self.assertTrue(payload["access_token"])
        self.assertTrue(payload["refresh_token"])
        self.assertEqual(payload["user"]["user_id"], "user-1")

    def test_login_rejects_wrong_password(self):
        async def seed():
            await self.store.upsert_user(
                tenant_id="demo",
                user_id="user-1",
                username="alice",
                password_hash=hash_password("secret"),
                roles=["user"],
            )
        asyncio.run(seed())

        with self.patch_auth(), mock.patch.dict("os.environ", {"JWT_SECRET_KEY": "test-secret-key"}):
            response = self.client.post(
                "/api/auth/login",
                json={"tenant_id": "demo", "username": "alice", "password": "wrong"},
            )

        self.assertEqual(response.status_code, 401)

    def test_refresh_rotates_token(self):
        async def seed():
            await self.store.upsert_user(
                tenant_id="demo",
                user_id="user-1",
                username="alice",
                password_hash=hash_password("secret"),
                roles=["user"],
            )
        asyncio.run(seed())

        with self.patch_auth(), mock.patch.dict("os.environ", {"JWT_SECRET_KEY": "test-secret-key"}):
            login = self.client.post(
                "/api/auth/login",
                json={"tenant_id": "demo", "username": "alice", "password": "secret"},
            ).json()
            response = self.client.post(
                "/api/auth/refresh",
                json={"refresh_token": login["refresh_token"]},
            )

        self.assertEqual(response.status_code, 200)
        self.assertNotEqual(response.json()["access_token"], login["access_token"])

    def test_me_returns_current_user(self):
        async def seed():
            await self.store.upsert_user(
                tenant_id="demo",
                user_id="user-1",
                username="alice",
                password_hash=hash_password("secret"),
                roles=["user"],
            )
        asyncio.run(seed())

        with self.patch_auth(), mock.patch.dict("os.environ", {"JWT_SECRET_KEY": "test-secret-key"}):
            login = self.client.post(
                "/api/auth/login",
                json={"tenant_id": "demo", "username": "alice", "password": "secret"},
            ).json()
            response = self.client.get(
                "/api/auth/me",
                headers={"Authorization": f"Bearer {login['access_token']}"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["username"], "alice")
```

Add missing imports at the top:

```python
import asyncio
from unittest import mock
```

- [ ] **Step 2: Run tests to verify failure**

Run: `python -m unittest tests.test_api_auth.ApiAuthRouteTests -v`

Expected: FAIL because auth routes do not exist.

- [ ] **Step 3: Implement auth endpoints**

In `api/routes.py`, import auth helpers and schemas:

```python
from api.auth import (
    AuthPrincipal,
    AuthSettings,
    create_access_token,
    create_refresh_token,
    decode_token,
    get_bearer_principal,
    load_auth_settings,
    verify_password,
)
from api.auth_store import create_auth_store, hash_refresh_token
from api.schemas import (
    AccessTokenResponse,
    AuthUserResponse,
    LoginRequest,
    LogoutRequest,
    LogoutResponse,
    RefreshRequest,
    TokenResponse,
)
```

Add route handlers:

```python
@router.post("/api/auth/login", response_model=TokenResponse)
async def login(request: LoginRequest):
    store = create_auth_store()
    settings = load_auth_settings()
    user = await store.find_user_by_login(
        tenant_id=request.tenant_id,
        username=request.username,
    )
    if not user or user["disabled"] or not verify_password(request.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid username or password")
    principal = _principal_from_user(user)
    access_token = create_access_token(principal, settings)
    refresh_token_id, refresh_token, refresh_expires_at = create_refresh_token(principal, settings)
    await store.store_refresh_token(
        token_id=refresh_token_id,
        token_hash=hash_refresh_token(refresh_token),
        tenant_id=principal.tenant_id,
        user_id=principal.user_id,
        expires_at=refresh_expires_at,
    )
    await store.record_login(tenant_id=principal.tenant_id, user_id=principal.user_id)
    return _token_response(access_token, refresh_token, principal, settings)


@router.post("/api/auth/refresh", response_model=TokenResponse)
async def refresh_token(request: RefreshRequest):
    store = create_auth_store()
    settings = load_auth_settings()
    refresh_principal = decode_token(
        request.refresh_token,
        expected_type="refresh",
        settings=settings,
    )
    token_hash = hash_refresh_token(request.refresh_token)
    refresh_record = await store.get_active_refresh_token(
        token_id=refresh_principal.token_id,
        token_hash=token_hash,
    )
    if refresh_record is None:
        raise HTTPException(status_code=401, detail="Invalid refresh token")
    user = await store.get_user(
        tenant_id=refresh_principal.tenant_id,
        user_id=refresh_principal.user_id,
    )
    if not user or user["disabled"] or user["token_version"] != refresh_principal.token_version:
        raise HTTPException(status_code=401, detail="Invalid refresh token")
    principal = _principal_from_user(user)
    access_token = create_access_token(principal, settings)
    new_token_id, new_refresh_token, refresh_expires_at = create_refresh_token(principal, settings)
    await store.rotate_refresh_token(
        old_token_id=refresh_principal.token_id,
        new_token_id=new_token_id,
        new_token_hash=hash_refresh_token(new_refresh_token),
        tenant_id=principal.tenant_id,
        user_id=principal.user_id,
        expires_at=refresh_expires_at,
    )
    return _token_response(access_token, new_refresh_token, principal, settings)


@router.get("/api/auth/me", response_model=AuthUserResponse)
async def me(principal: AuthPrincipal = Depends(get_bearer_principal)):
    return _user_response(principal)


@router.post("/api/auth/logout", response_model=LogoutResponse)
async def logout(request: LogoutRequest):
    if request.refresh_token:
        refresh_principal = decode_token(request.refresh_token, expected_type="refresh")
        await create_auth_store().revoke_refresh_token(
            token_id=refresh_principal.token_id,
            token_hash=hash_refresh_token(request.refresh_token),
        )
    return {"ok": True}
```

Add helper functions near the bottom of `api/routes.py`:

```python
def _principal_from_user(user: dict[str, object]) -> AuthPrincipal:
    return AuthPrincipal(
        tenant_id=str(user["tenant_id"]),
        user_id=str(user["user_id"]),
        username=str(user["username"]),
        roles=[str(role) for role in user.get("roles", [])],
        token_version=int(user.get("token_version", 0)),
        token_id="",
    )


def _user_response(principal: AuthPrincipal) -> dict[str, object]:
    return {
        "tenant_id": principal.tenant_id,
        "user_id": principal.user_id,
        "username": principal.username,
        "roles": principal.roles,
    }


def _token_response(
    access_token: str,
    refresh_token: str,
    principal: AuthPrincipal,
    settings: AuthSettings,
) -> dict[str, object]:
    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": "bearer",
        "expires_in": settings.access_token_expire_minutes * 60,
        "user": _user_response(principal),
    }
```

- [ ] **Step 4: Run tests to verify pass**

Run: `python -m unittest tests.test_api_auth.ApiAuthRouteTests -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add api/routes.py tests/test_api_auth.py
git commit -m "feat: add auth routes"
```

## Task 6: Protect Existing API Routes

**Files:**
- Modify: `api/routes.py`
- Modify: `tests/test_api_nl2sql.py`
- Modify: `tests/test_api_conversations.py`

- [ ] **Step 1: Add token helper to API tests**

In both `tests/test_api_nl2sql.py` and `tests/test_api_conversations.py`, add:

```python
from api.auth import AuthPrincipal, AuthSettings, create_access_token


def auth_headers(tenant_id="demo", user_id="user-1", username="alice"):
    token = create_access_token(
        AuthPrincipal(
            tenant_id=tenant_id,
            user_id=user_id,
            username=username,
            roles=["user"],
            token_version=0,
            token_id="test-token",
        ),
        AuthSettings(secret_key="test-secret-key"),
    )
    return {"Authorization": f"Bearer {token}"}
```

Wrap protected route calls with `headers=auth_headers()` and patch `JWT_SECRET_KEY` where needed:

```python
with mock.patch.dict("os.environ", {"JWT_SECRET_KEY": "test-secret-key"}):
    response = client.post("/api/nl2sql", json={"question": "show gmv"}, headers=auth_headers())
```

- [ ] **Step 2: Add missing-token and mismatch tests**

In `tests/test_api_nl2sql.py`, add:

```python
def test_nl2sql_requires_access_token(self):
    client = TestClient(create_app())

    response = client.post("/api/nl2sql", json={"question": "show gmv"})

    self.assertEqual(response.status_code, 401)


def test_nl2sql_rejects_mismatched_tenant(self):
    client = TestClient(create_app())

    with mock.patch.dict("os.environ", {"JWT_SECRET_KEY": "test-secret-key"}):
        response = client.post(
            "/api/nl2sql",
            json={"question": "show gmv", "tenant_id": "other"},
            headers=auth_headers(tenant_id="demo"),
        )

    self.assertEqual(response.status_code, 403)
```

In `tests/test_api_conversations.py`, add a test that creates a conversation using token identity and no request `user_id`, then asserts the stored `user_id` is `user-1`.

- [ ] **Step 3: Run tests to verify failure**

Run:

```powershell
python -m unittest tests.test_api_nl2sql tests.test_api_conversations -v
```

Expected: FAIL because existing routes are not protected and request identity is still used directly.

- [ ] **Step 4: Add identity dependency and enforce principal**

In `api/routes.py`, add:

```python
def _resolve_tenant(principal: AuthPrincipal, requested_tenant_id: str | None) -> str:
    if requested_tenant_id and requested_tenant_id != principal.tenant_id:
        raise HTTPException(status_code=403, detail="Authenticated tenant does not match request tenant")
    return principal.tenant_id


def _resolve_user(principal: AuthPrincipal, requested_user_id: str | None) -> str:
    if requested_user_id and requested_user_id != principal.user_id:
        raise HTTPException(status_code=403, detail="Authenticated user does not match request user")
    return principal.user_id
```

Add `principal: AuthPrincipal = Depends(get_bearer_principal)` to every `/api/nl2sql` and `/api/conversations*` handler. Replace direct `request.tenant_id`, `request.user_id`, and query identity values with resolved values:

```python
tenant_id = _resolve_tenant(principal, request.tenant_id)
user_id = _resolve_user(principal, request.user_id)
```

For query endpoints, change query parameters to optional:

```python
tenant_id: str | None = Query(default=None, min_length=1)
user_id: str | None = Query(default=None, min_length=1)
```

Then pass resolved identity to `create_conversation_store()` operations and `run_nl2sql()`.

- [ ] **Step 5: Run protected API tests to verify pass**

Run:

```powershell
python -m unittest tests.test_api_nl2sql tests.test_api_conversations -v
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add api/routes.py tests/test_api_nl2sql.py tests/test_api_conversations.py
git commit -m "feat: require jwt for api routes"
```

## Task 7: User Provisioning Script

**Files:**
- Create: `scripts/create_auth_user.py`
- Test: `tests/test_api_auth.py`

- [ ] **Step 1: Add script-level test for argument behavior**

Add to `tests/test_api_auth.py`:

```python
class CreateAuthUserScriptTests(unittest.TestCase):
    def test_build_user_payload_hashes_password(self):
        from scripts.create_auth_user import build_user_payload

        payload = build_user_payload(
            tenant_id="demo",
            user_id="user-1",
            username="alice",
            password="secret",
            roles=["user", "admin"],
        )

        self.assertEqual(payload["tenant_id"], "demo")
        self.assertEqual(payload["user_id"], "user-1")
        self.assertEqual(payload["username"], "alice")
        self.assertEqual(payload["roles"], ["user", "admin"])
        self.assertNotEqual(payload["password_hash"], "secret")
```

- [ ] **Step 2: Run test to verify failure**

Run: `python -m unittest tests.test_api_auth.CreateAuthUserScriptTests -v`

Expected: FAIL because `scripts.create_auth_user` does not exist.

- [ ] **Step 3: Implement the script**

Create `scripts/create_auth_user.py`:

```python
from __future__ import annotations

import argparse
import asyncio

from api.auth import hash_password
from api.auth_store import create_auth_store


def build_user_payload(
    *,
    tenant_id: str,
    user_id: str,
    username: str,
    password: str,
    roles: list[str],
) -> dict[str, object]:
    return {
        "tenant_id": tenant_id.strip(),
        "user_id": user_id.strip(),
        "username": username.strip(),
        "password_hash": hash_password(password),
        "roles": roles,
    }


async def create_user(args: argparse.Namespace) -> None:
    store = create_auth_store()
    payload = build_user_payload(
        tenant_id=args.tenant_id,
        user_id=args.user_id,
        username=args.username,
        password=args.password,
        roles=args.roles,
    )
    await store.upsert_user(
        tenant_id=str(payload["tenant_id"]),
        user_id=str(payload["user_id"]),
        username=str(payload["username"]),
        password_hash=str(payload["password_hash"]),
        roles=list(payload["roles"]),
        disabled=args.disabled,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create or update an API auth user.")
    parser.add_argument("--tenant-id", default="demo")
    parser.add_argument("--user-id", required=True)
    parser.add_argument("--username", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument("--roles", nargs="+", default=["user"])
    parser.add_argument("--disabled", action="store_true")
    return parser.parse_args()


def main() -> None:
    asyncio.run(create_user(parse_args()))


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run test to verify pass**

Run: `python -m unittest tests.test_api_auth.CreateAuthUserScriptTests -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/create_auth_user.py tests/test_api_auth.py
git commit -m "feat: add auth user provisioning script"
```

## Task 8: Configuration And Documentation

**Files:**
- Modify: `.env.example`
- Modify: `README.md`

- [ ] **Step 1: Update `.env.example`**

Add:

```env
# Auth database defaults to MEMORY_DATABASE_URL when blank.
AUTH_DATABASE_URL=
JWT_SECRET_KEY=
JWT_ALGORITHM=HS256
JWT_ACCESS_TOKEN_EXPIRE_MINUTES=30
JWT_REFRESH_TOKEN_EXPIRE_DAYS=7
JWT_ISSUER=nl2sql-api
JWT_AUDIENCE=nl2sql-client
```

- [ ] **Step 2: Update README**

Add an authentication section:

````markdown
## API Authentication

Apply the auth schema to the service metadata database:

```powershell
psql "$env:MEMORY_DATABASE_URL" -f db/auth.sql
```

Set `JWT_SECRET_KEY` in `.env`, then create a user:

```powershell
python scripts/create_auth_user.py --tenant-id demo --user-id user-1 --username alice --password "secret"
```

Login:

```powershell
curl -X POST http://127.0.0.1:8000/api/auth/login `
  -H "Content-Type: application/json" `
  -d "{\"tenant_id\":\"demo\",\"username\":\"alice\",\"password\":\"secret\"}"
```

Call protected endpoints with the access token:

```powershell
curl -X POST http://127.0.0.1:8000/api/nl2sql `
  -H "Content-Type: application/json" `
  -H "Authorization: Bearer <access_token>" `
  -d "{\"question\":\"show gmv\",\"execute\":false}"
```
````

- [ ] **Step 3: Commit**

```bash
git add .env.example README.md
git commit -m "docs: document api authentication"
```

## Task 9: Full Verification

**Files:**
- All changed files

- [ ] **Step 1: Run focused auth tests**

Run:

```powershell
python -m unittest tests.test_auth_tokens tests.test_auth_store tests.test_api_auth -v
```

Expected: all tests PASS.

- [ ] **Step 2: Run focused API regression tests**

Run:

```powershell
python -m unittest tests.test_api_health tests.test_api_nl2sql tests.test_api_conversations -v
```

Expected: all tests PASS. `GET /health` remains public. Protected endpoints require valid bearer tokens.

- [ ] **Step 3: Run full suite**

Run:

```powershell
python -m unittest discover -s tests -v
```

Expected: full suite PASS.

- [ ] **Step 4: Inspect git diff**

Run:

```bash
git diff --stat HEAD
git diff --check
```

Expected: diff contains only auth-related code, tests, SQL, and docs. `git diff --check` reports no whitespace errors.

- [ ] **Step 5: Final commit if verification changed files**

If verification or docs edits changed files after the previous commits:

```bash
git add api db scripts tests .env.example README.md pyproject.toml
git commit -m "test: verify api authentication"
```

## Self-Review

- Spec coverage: database users, refresh-token storage, token rotation, protected endpoints, backwards-compatible identity fields, provisioning, and docs all map to tasks.
- Red-flag scan: no vague implementation instructions remain; every task names exact files, tests, commands, and expected outcomes.
- Type consistency: `AuthPrincipal`, `AuthSettings`, `LoginRequest`, `RefreshRequest`, `TokenResponse`, and store method names are used consistently across tasks.
