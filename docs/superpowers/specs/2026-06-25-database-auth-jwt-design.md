# Database User Table JWT Auth Design

> **Retained subsystem:** Authentication and server-owned principal identity remain current security boundaries for the native Analysis Agent.

## Problem

The current FastAPI API trusts `tenant_id` and `user_id` values supplied by each request. That is acceptable only behind a trusted caller. Once the API is exposed to real users, any client can impersonate another tenant or user by changing request JSON or query parameters.

The API needs first-party login state, signed access tokens, refresh-token rotation, and server-side session revocation while preserving the current NL2SQL and conversation behavior.

## Goals

- Add database-backed users for API authentication.
- Issue short-lived JWT access tokens and long-lived refresh tokens.
- Store refresh-token state server-side so logout, token rotation, and revocation work.
- Derive trusted `tenant_id` and `user_id` from the authenticated principal.
- Keep existing endpoint shapes mostly compatible during migration.
- Cover authentication, refresh rotation, authorization, and existing API regressions with tests.

## Non-Goals

- No frontend login UI.
- No self-service registration endpoint.
- No OAuth/OIDC, SSO, MFA, password reset, or email verification.
- No role-based feature authorization beyond carrying roles in the principal.
- No changes to NL2SQL graph behavior, SQL validation, RAG, or memory semantics.

## Architecture

Add an authentication layer under `api/` with three focused responsibilities:

- `api.auth`: password hashing, JWT signing/verification, token settings, FastAPI authentication dependencies.
- `api.auth_store`: database and test stores for users and refresh-token sessions.
- `api.routes`: auth endpoints plus protected NL2SQL/conversation endpoints.

The auth database tables live with service metadata, not the business query database. The store resolves its DSN from `AUTH_DATABASE_URL`, then `MEMORY_DATABASE_URL`, then `MEMORY_POSTGRES_DSN`. This keeps auth close to existing conversation metadata while still allowing a separate auth database later.

## Database Schema

Create `db/auth.sql`.

`auth_users` stores login identities:

- `tenant_id TEXT NOT NULL`
- `user_id TEXT NOT NULL`
- `username TEXT NOT NULL`
- `password_hash TEXT NOT NULL`
- `roles JSONB NOT NULL DEFAULT '["user"]'::jsonb`
- `disabled BOOLEAN NOT NULL DEFAULT false`
- `token_version INTEGER NOT NULL DEFAULT 0`
- `created_at`, `updated_at`, `last_login_at`
- Unique constraints on `(tenant_id, user_id)` and `(tenant_id, username)`.

`auth_refresh_tokens` stores refresh-token sessions:

- `token_id TEXT NOT NULL UNIQUE`, matching JWT `jti`
- `token_hash TEXT NOT NULL UNIQUE`, SHA-256 of the full refresh token string
- `tenant_id`, `user_id`
- `expires_at`, `created_at`, `last_used_at`
- `revoked_at`
- `replaced_by_token_id`
- Optional `user_agent`, `client_ip`
- Foreign key to `auth_users(tenant_id, user_id)`.

Refresh tokens are never stored raw. A leaked database row must not be usable as a bearer credential.

## Token Model

Access tokens are signed JWTs with short TTL:

```json
{
  "typ": "access",
  "sub": "user-1",
  "tenant_id": "demo",
  "username": "alice",
  "roles": ["user"],
  "ver": 0,
  "iss": "nl2sql-api",
  "aud": "nl2sql-client",
  "iat": 1782360000,
  "exp": 1782361800,
  "jti": "uuid"
}
```

Refresh tokens are also signed JWTs but only carry session identity:

```json
{
  "typ": "refresh",
  "sub": "user-1",
  "tenant_id": "demo",
  "ver": 0,
  "iss": "nl2sql-api",
  "aud": "nl2sql-client",
  "iat": 1782360000,
  "exp": 1782964800,
  "jti": "uuid"
}
```

`JWT_SECRET_KEY` is required. The default algorithm is `HS256`. Access tokens default to 30 minutes. Refresh tokens default to 7 days.

## API Contract

### `POST /api/auth/login`

Request:

```json
{
  "tenant_id": "demo",
  "username": "alice",
  "password": "secret"
}
```

Response:

```json
{
  "access_token": "...",
  "refresh_token": "...",
  "token_type": "bearer",
  "expires_in": 1800,
  "user": {
    "tenant_id": "demo",
    "user_id": "user-1",
    "username": "alice",
    "roles": ["user"]
  }
}
```

Invalid credentials return `401` with a generic message. Disabled users return the same `401` shape to avoid user-state enumeration.

### `POST /api/auth/refresh`

Request:

```json
{
  "refresh_token": "..."
}
```

Behavior:

- Decode and validate a `typ=refresh` token.
- Hash the raw refresh token and find an active database row.
- Validate expiration, revocation state, user disabled state, and `token_version`.
- Revoke the old refresh row, insert a new refresh row, and return a new token pair.

Refresh reuse after rotation returns `401`. The first implementation does not need automatic family-wide revocation on reuse, but the schema supports it later.

### `GET /api/auth/me`

Requires an access token and returns the current authenticated user.

### `POST /api/auth/logout`

Request may include the current refresh token:

```json
{
  "refresh_token": "..."
}
```

If supplied and active, the matching refresh-token row is revoked. The client must discard both access and refresh tokens. Existing access tokens remain valid until they expire unless user `token_version` is changed by an admin operation.

## Protected Existing Endpoints

The following endpoints require `Authorization: Bearer <access_token>`:

- `POST /api/nl2sql`
- `POST /api/conversations`
- `GET /api/conversations`
- `GET /api/conversations/{conversation_id}`
- `PATCH /api/conversations/{conversation_id}`
- `GET /api/conversations/{conversation_id}/messages`
- `POST /api/conversations/{conversation_id}/messages`

`GET /health`, `POST /api/auth/login`, and `POST /api/auth/refresh` remain public.

The token principal is the source of truth. Existing `tenant_id` and `user_id` request fields are accepted for backward compatibility only when they match the token. Missing identity fields are filled from the token. Mismatched identity returns `403`.

Examples:

- Token `tenant_id=demo,user_id=user-1`; request omits both values: use `demo,user-1`.
- Token `tenant_id=demo,user_id=user-1`; request sends same values: allowed.
- Token `tenant_id=demo,user_id=user-1`; request sends `user_id=user-2`: `403`.

## Error Handling

- Missing, malformed, expired, wrong-type, or invalid-signature access tokens return `401`.
- Auth responses include `WWW-Authenticate: Bearer` where applicable.
- Authenticated identity mismatch returns `403`.
- Unknown conversations remain `404` to preserve isolation semantics.
- Internal store/configuration failures continue through the existing `_internal_error_response` pattern unless they are expected authentication failures.

## Configuration

Add to `.env.example`:

```env
AUTH_DATABASE_URL=
JWT_SECRET_KEY=
JWT_ALGORITHM=HS256
JWT_ACCESS_TOKEN_EXPIRE_MINUTES=30
JWT_REFRESH_TOKEN_EXPIRE_DAYS=7
JWT_ISSUER=nl2sql-api
JWT_AUDIENCE=nl2sql-client
```

`AUTH_DATABASE_URL` is optional when `MEMORY_DATABASE_URL` is already configured.

## User Provisioning

No registration endpoint is added. Add `scripts/create_auth_user.py` so operators can create or update users:

```powershell
python scripts/create_auth_user.py --tenant-id demo --user-id user-1 --username alice --password "..."
```

The script hashes the password and upserts into `auth_users`.

## Testing

Regression tests must cover:

1. Password hashing verifies correct and incorrect passwords.
2. Access tokens decode only as access tokens.
3. Refresh tokens decode only as refresh tokens.
4. Login succeeds for an active user and returns token pair plus user.
5. Login rejects unknown user, wrong password, and disabled user.
6. `/api/auth/me` rejects missing/invalid tokens and returns the authenticated principal for valid tokens.
7. Refresh rotates tokens, revokes the old refresh row, and rejects reuse.
8. Logout revokes the supplied refresh token.
9. Protected endpoints reject missing access token with `401`.
10. Protected endpoints reject mismatched `tenant_id` or `user_id` with `403`.
11. Protected endpoints continue to call `run_nl2sql` and conversation store with the authenticated identity.

Existing health tests remain public. Existing API tests should use a token helper instead of bypassing authentication.

## Rollout

1. Apply `db/auth.sql` to the service metadata database.
2. Set `JWT_SECRET_KEY`.
3. Create at least one auth user with `scripts/create_auth_user.py`.
4. Update clients to call `/api/auth/login` and send `Authorization: Bearer`.
5. Keep sending old `tenant_id/user_id` fields only while migrating clients; remove them from clients after validation.

## Scope Boundaries

This change secures API identity and refresh-token session state. It does not implement tenant admin features, per-role authorization policies, or user lifecycle management beyond script-based provisioning.
