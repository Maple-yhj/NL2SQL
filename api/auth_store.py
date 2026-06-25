from __future__ import annotations

import hashlib
import json
import os
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

import asyncpg

from core.environment import load_project_environment


AuthUser = dict[str, Any]
RefreshTokenRecord = dict[str, Any]


@runtime_checkable
class AuthStoreProtocol(Protocol):
    async def find_user_by_login(self, tenant_id: str, username: str) -> AuthUser | None:
        ...

    async def get_user(self, tenant_id: str, user_id: str) -> AuthUser | None:
        ...

    async def upsert_user(
        self,
        tenant_id: str,
        user_id: str,
        username: str,
        password_hash: str,
        roles: list[str],
        disabled: bool = False,
    ) -> AuthUser:
        ...

    async def record_login(self, tenant_id: str, user_id: str) -> None:
        ...

    async def store_refresh_token(
        self,
        token_id: str,
        token_hash: str,
        tenant_id: str,
        user_id: str,
        expires_at: datetime,
        user_agent: str = "",
        client_ip: str = "",
    ) -> RefreshTokenRecord:
        ...

    async def get_active_refresh_token(
        self, token_id: str, token_hash: str
    ) -> RefreshTokenRecord | None:
        ...

    async def rotate_refresh_token(
        self,
        old_token_id: str,
        old_token_hash: str,
        new_token_id: str,
        new_token_hash: str,
        tenant_id: str,
        user_id: str,
        expires_at: datetime,
        user_agent: str = "",
        client_ip: str = "",
    ) -> RefreshTokenRecord:
        ...

    async def revoke_refresh_token(self, token_id: str, token_hash: str) -> None:
        ...


def hash_refresh_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class InMemoryAuthStore:
    def __init__(self) -> None:
        self._users: dict[tuple[str, str], AuthUser] = {}
        self._users_by_login: dict[tuple[str, str], tuple[str, str]] = {}
        self._refresh_tokens: dict[str, RefreshTokenRecord] = {}
        self._refresh_token_hashes: dict[str, str] = {}

    async def find_user_by_login(self, tenant_id: str, username: str) -> AuthUser | None:
        user_key = self._users_by_login.get((tenant_id, username))
        if user_key is None:
            return None
        user = self._users.get(user_key)
        return deepcopy(user) if user else None

    async def get_user(self, tenant_id: str, user_id: str) -> AuthUser | None:
        user = self._users.get((tenant_id, user_id))
        return deepcopy(user) if user else None

    async def upsert_user(
        self,
        tenant_id: str,
        user_id: str,
        username: str,
        password_hash: str,
        roles: list[str],
        disabled: bool = False,
    ) -> AuthUser:
        now = _utc_now()
        key = (tenant_id, user_id)
        existing = self._users.get(key)
        login_key = (tenant_id, username)
        login_owner = self._users_by_login.get(login_key)
        if login_owner is not None and login_owner != key:
            raise ValueError("Username already exists for tenant")

        if existing:
            old_login_key = (tenant_id, existing["username"])
            if old_login_key != login_key:
                self._users_by_login.pop(old_login_key, None)
            user = {
                **existing,
                "username": username,
                "password_hash": password_hash,
                "roles": list(roles),
                "disabled": bool(disabled),
                "updated_at": now,
            }
        else:
            user = {
                "tenant_id": tenant_id,
                "user_id": user_id,
                "username": username,
                "password_hash": password_hash,
                "roles": list(roles),
                "disabled": bool(disabled),
                "token_version": 0,
                "created_at": now,
                "updated_at": now,
                "last_login_at": None,
            }
        self._users[key] = deepcopy(user)
        self._users_by_login[login_key] = key
        return deepcopy(user)

    async def record_login(self, tenant_id: str, user_id: str) -> None:
        user = self._users.get((tenant_id, user_id))
        if user:
            now = _utc_now()
            user["last_login_at"] = now
            user["updated_at"] = now

    async def store_refresh_token(
        self,
        token_id: str,
        token_hash: str,
        tenant_id: str,
        user_id: str,
        expires_at: datetime,
        user_agent: str = "",
        client_ip: str = "",
    ) -> RefreshTokenRecord:
        if token_id in self._refresh_tokens:
            raise ValueError("Refresh token id already exists")
        if token_hash in self._refresh_token_hashes:
            raise ValueError("Refresh token hash already exists")

        now = _utc_now()
        record = {
            "token_id": token_id,
            "token_hash": token_hash,
            "tenant_id": tenant_id,
            "user_id": user_id,
            "expires_at": expires_at,
            "revoked_at": None,
            "replaced_by_token_id": None,
            "user_agent": user_agent,
            "client_ip": client_ip,
            "created_at": now,
            "last_used_at": None,
        }
        self._refresh_tokens[token_id] = deepcopy(record)
        self._refresh_token_hashes[token_hash] = token_id
        return deepcopy(record)

    async def get_active_refresh_token(
        self, token_id: str, token_hash: str
    ) -> RefreshTokenRecord | None:
        record = self._refresh_tokens.get(token_id)
        if not record or record["token_hash"] != token_hash:
            return None
        if record["revoked_at"] is not None or _is_expired(record["expires_at"]):
            return None
        record["last_used_at"] = _utc_now()
        return deepcopy(record)

    async def rotate_refresh_token(
        self,
        old_token_id: str,
        old_token_hash: str,
        new_token_id: str,
        new_token_hash: str,
        tenant_id: str,
        user_id: str,
        expires_at: datetime,
        user_agent: str = "",
        client_ip: str = "",
    ) -> RefreshTokenRecord:
        old_record = self._refresh_tokens.get(old_token_id)
        if (
            not old_record
            or old_record["token_hash"] != old_token_hash
            or old_record["tenant_id"] != tenant_id
            or old_record["user_id"] != user_id
            or old_record["revoked_at"] is not None
            or _is_expired(old_record["expires_at"])
        ):
            raise ValueError("Old refresh token is not active for this user")
        if new_token_id in self._refresh_tokens:
            raise ValueError("Refresh token id already exists")
        if new_token_hash in self._refresh_token_hashes:
            raise ValueError("Refresh token hash already exists")

        old_record["revoked_at"] = _utc_now()
        old_record["replaced_by_token_id"] = new_token_id
        return await self.store_refresh_token(
            new_token_id,
            new_token_hash,
            tenant_id,
            user_id,
            expires_at,
            user_agent,
            client_ip,
        )

    async def revoke_refresh_token(self, token_id: str, token_hash: str) -> None:
        record = self._refresh_tokens.get(token_id)
        if record and record["token_hash"] == token_hash and record["revoked_at"] is None:
            record["revoked_at"] = _utc_now()


class PostgresAuthStore:
    def __init__(self, dsn: str | None = None) -> None:
        self.dsn = dsn

    def _resolve_dsn(self) -> str:
        dsn = self.dsn or _auth_dsn_from_env()
        if not dsn:
            raise RuntimeError(
                "Missing AUTH_DATABASE_URL, MEMORY_DATABASE_URL, or MEMORY_POSTGRES_DSN for auth store."
            )
        return dsn

    async def _connect(self):
        return await asyncpg.connect(self._resolve_dsn(), ssl=False)

    async def find_user_by_login(self, tenant_id: str, username: str) -> AuthUser | None:
        conn = await self._connect()
        try:
            row = await conn.fetchrow(
                """
                SELECT tenant_id, user_id, username, password_hash, roles, disabled,
                       token_version, created_at, updated_at, last_login_at
                FROM auth_users
                WHERE tenant_id = $1 AND username = $2
                """,
                tenant_id,
                username,
            )
        finally:
            await conn.close()
        return _user_from_row(row) if row else None

    async def get_user(self, tenant_id: str, user_id: str) -> AuthUser | None:
        conn = await self._connect()
        try:
            row = await conn.fetchrow(
                """
                SELECT tenant_id, user_id, username, password_hash, roles, disabled,
                       token_version, created_at, updated_at, last_login_at
                FROM auth_users
                WHERE tenant_id = $1 AND user_id = $2
                """,
                tenant_id,
                user_id,
            )
        finally:
            await conn.close()
        return _user_from_row(row) if row else None

    async def upsert_user(
        self,
        tenant_id: str,
        user_id: str,
        username: str,
        password_hash: str,
        roles: list[str],
        disabled: bool = False,
    ) -> AuthUser:
        conn = await self._connect()
        try:
            row = await conn.fetchrow(
                """
                INSERT INTO auth_users
                    (tenant_id, user_id, username, password_hash, roles, disabled, updated_at)
                VALUES ($1, $2, $3, $4, $5::jsonb, $6, now())
                ON CONFLICT (tenant_id, user_id)
                DO UPDATE SET username = EXCLUDED.username,
                              password_hash = EXCLUDED.password_hash,
                              roles = EXCLUDED.roles,
                              disabled = EXCLUDED.disabled,
                              updated_at = now()
                RETURNING tenant_id, user_id, username, password_hash, roles, disabled,
                          token_version, created_at, updated_at, last_login_at
                """,
                tenant_id,
                user_id,
                username,
                password_hash,
                json.dumps(roles, ensure_ascii=False),
                disabled,
            )
        finally:
            await conn.close()
        return _user_from_row(row)

    async def record_login(self, tenant_id: str, user_id: str) -> None:
        conn = await self._connect()
        try:
            await conn.execute(
                """
                UPDATE auth_users
                SET last_login_at = now(), updated_at = now()
                WHERE tenant_id = $1 AND user_id = $2
                """,
                tenant_id,
                user_id,
            )
        finally:
            await conn.close()

    async def store_refresh_token(
        self,
        token_id: str,
        token_hash: str,
        tenant_id: str,
        user_id: str,
        expires_at: datetime,
        user_agent: str = "",
        client_ip: str = "",
    ) -> RefreshTokenRecord:
        conn = await self._connect()
        try:
            row = await conn.fetchrow(
                """
                INSERT INTO auth_refresh_tokens
                    (token_id, token_hash, tenant_id, user_id, expires_at, user_agent, client_ip)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                RETURNING token_id, token_hash, tenant_id, user_id, expires_at, revoked_at,
                          replaced_by_token_id, user_agent, client_ip, created_at, last_used_at
                """,
                token_id,
                token_hash,
                tenant_id,
                user_id,
                expires_at,
                user_agent,
                client_ip,
            )
        finally:
            await conn.close()
        return _refresh_token_from_row(row)

    async def get_active_refresh_token(
        self, token_id: str, token_hash: str
    ) -> RefreshTokenRecord | None:
        conn = await self._connect()
        try:
            row = await conn.fetchrow(
                """
                UPDATE auth_refresh_tokens
                SET last_used_at = now()
                WHERE token_id = $1
                  AND token_hash = $2
                  AND revoked_at IS NULL
                  AND expires_at > now()
                RETURNING token_id, token_hash, tenant_id, user_id, expires_at, revoked_at,
                          replaced_by_token_id, user_agent, client_ip, created_at, last_used_at
                """,
                token_id,
                token_hash,
            )
        finally:
            await conn.close()
        return _refresh_token_from_row(row) if row else None

    async def rotate_refresh_token(
        self,
        old_token_id: str,
        old_token_hash: str,
        new_token_id: str,
        new_token_hash: str,
        tenant_id: str,
        user_id: str,
        expires_at: datetime,
        user_agent: str = "",
        client_ip: str = "",
    ) -> RefreshTokenRecord:
        conn = await self._connect()
        try:
            async with conn.transaction():
                updated = await conn.fetchrow(
                    """
                    UPDATE auth_refresh_tokens
                    SET revoked_at = now(),
                        replaced_by_token_id = $2
                    WHERE token_id = $1
                      AND token_hash = $3
                      AND tenant_id = $4
                      AND user_id = $5
                      AND revoked_at IS NULL
                      AND expires_at > now()
                    RETURNING token_id
                    """,
                    old_token_id,
                    new_token_id,
                    old_token_hash,
                    tenant_id,
                    user_id,
                )
                if not updated:
                    raise ValueError("Old refresh token is not active for this user")

                row = await conn.fetchrow(
                    """
                    INSERT INTO auth_refresh_tokens
                        (token_id, token_hash, tenant_id, user_id, expires_at, user_agent, client_ip)
                    VALUES ($1, $2, $3, $4, $5, $6, $7)
                    RETURNING token_id, token_hash, tenant_id, user_id, expires_at, revoked_at,
                              replaced_by_token_id, user_agent, client_ip, created_at, last_used_at
                    """,
                    new_token_id,
                    new_token_hash,
                    tenant_id,
                    user_id,
                    expires_at,
                    user_agent,
                    client_ip,
                )
        finally:
            await conn.close()
        return _refresh_token_from_row(row)

    async def revoke_refresh_token(self, token_id: str, token_hash: str) -> None:
        conn = await self._connect()
        try:
            await conn.execute(
                """
                UPDATE auth_refresh_tokens
                SET revoked_at = COALESCE(revoked_at, now())
                WHERE token_id = $1 AND token_hash = $2
                """,
                token_id,
                token_hash,
            )
        finally:
            await conn.close()


def create_auth_store(dsn: str | None = None) -> AuthStoreProtocol:
    auth_dsn = dsn or _auth_dsn_from_env()
    if not auth_dsn:
        raise RuntimeError(
            "Missing AUTH_DATABASE_URL, MEMORY_DATABASE_URL, or MEMORY_POSTGRES_DSN for auth store."
        )
    return PostgresAuthStore(auth_dsn)


def _auth_dsn_from_env() -> str | None:
    load_project_environment()
    return (
        os.getenv("AUTH_DATABASE_URL")
        or os.getenv("MEMORY_DATABASE_URL")
        or os.getenv("MEMORY_POSTGRES_DSN")
    )


def _decode_json_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        return json.loads(value)
    return list(value)


def _user_from_row(row: Any) -> AuthUser:
    return {
        "tenant_id": row["tenant_id"],
        "user_id": row["user_id"],
        "username": row["username"],
        "password_hash": row["password_hash"],
        "roles": _decode_json_list(row["roles"]),
        "disabled": bool(row["disabled"]),
        "token_version": int(row["token_version"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "last_login_at": row["last_login_at"],
    }


def _refresh_token_from_row(row: Any) -> RefreshTokenRecord:
    return {
        "token_id": row["token_id"],
        "token_hash": row["token_hash"],
        "tenant_id": row["tenant_id"],
        "user_id": row["user_id"],
        "expires_at": row["expires_at"],
        "revoked_at": row["revoked_at"],
        "replaced_by_token_id": row["replaced_by_token_id"],
        "user_agent": row["user_agent"] or "",
        "client_ip": row["client_ip"] or "",
        "created_at": row["created_at"],
        "last_used_at": row["last_used_at"],
    }


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _is_expired(expires_at: datetime) -> bool:
    now = _utc_now()
    if expires_at.tzinfo is None:
        now = now.replace(tzinfo=None)
    return expires_at <= now
