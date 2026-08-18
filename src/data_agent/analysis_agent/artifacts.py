"""Tenant-scoped JSON Artifact Store with integrity and retention controls."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import sqlite3
import tempfile
from collections.abc import Callable
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from pydantic import Field, JsonValue, field_validator
from pydantic_core import to_jsonable_python

from data_agent.public_contracts import (
    NonBlankText,
    PublicContractModel,
    SchemaFingerprint,
)
from data_agent.runtime.errors import ErrorCode

from .models import AgentArtifactKind, AgentArtifactRef


_SAFE_CALL_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_:-]*$")
_ARTIFACT_ID_PATTERN = re.compile(r"^artifact-[0-9a-f]{64}$")
_SENSITIVE_KEY_MARKERS = (
    "address",
    "api_key",
    "credential",
    "dsn",
    "email",
    "phone",
    "secret",
    "password",
    "token",
)


class ArtifactStoreError(ValueError):
    def __init__(self, code: ErrorCode, message: str) -> None:
        self.code = code
        super().__init__(message)


class _ArtifactWrite(PublicContractModel):
    tenant_id: NonBlankText
    user_id: NonBlankText
    run_id: NonBlankText
    call_id: str = Field(min_length=1, max_length=160)
    kind: AgentArtifactKind
    payload: JsonValue
    schema_digest: SchemaFingerprint | None = None
    row_count: int | None = Field(default=None, ge=0)
    sensitivity: Literal["metadata", "derived", "row_data"]
    retention_seconds: int = Field(default=86_400, ge=1)
    retained: bool = False

    @field_validator("call_id")
    @classmethod
    def validate_call_id(cls, value: str) -> str:
        call_id = value.strip()
        if _SAFE_CALL_ID_PATTERN.fullmatch(call_id) is None:
            raise ValueError("call_id must be a server-safe identifier without a path or extension")
        return call_id


def _canonical_json_bytes(payload: JsonValue) -> bytes:
    return json.dumps(
        to_jsonable_python(payload),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _owner_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]


class SQLiteArtifactStore:
    """Persist JSON payloads outside checkpoints and refs inside SQLite metadata."""

    def __init__(
        self,
        state_root: str | Path,
        *,
        clock: Callable[[], datetime] | None = None,
        atomic_replace: Callable[[str | Path, str | Path], None] = os.replace,
        max_artifact_bytes: int = 64 * 1024 * 1024,
    ) -> None:
        if max_artifact_bytes < 1:
            raise ValueError("max_artifact_bytes must be positive")
        self._state_root = Path(state_root).expanduser().resolve()
        self._database_path = self._state_root / "control" / "agent-artifacts.sqlite3"
        self._payload_root = self._state_root / "artifacts"
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        self._payload_root.mkdir(parents=True, exist_ok=True)
        self._clock = clock or (lambda: datetime.now(UTC))
        self._atomic_replace = atomic_replace
        self._max_artifact_bytes = max_artifact_bytes
        self._lock = asyncio.Lock()
        self._initialize()

    @property
    def database_path(self) -> Path:
        return self._database_path

    @property
    def payload_root(self) -> Path:
        return self._payload_root

    async def put_json(
        self,
        *,
        tenant_id: str,
        user_id: str,
        run_id: str,
        call_id: str,
        kind: AgentArtifactKind | str,
        payload: JsonValue,
        sensitivity: Literal["metadata", "derived", "row_data"],
        schema_digest: str | None = None,
        row_count: int | None = None,
        retention_seconds: int = 86_400,
        retained: bool = False,
    ) -> AgentArtifactRef:
        request = _ArtifactWrite.model_validate(
            {
                "tenant_id": tenant_id,
                "user_id": user_id,
                "run_id": run_id,
                "call_id": call_id,
                "kind": kind,
                "payload": payload,
                "schema_digest": schema_digest,
                "row_count": row_count,
                "sensitivity": sensitivity,
                "retention_seconds": retention_seconds,
                "retained": retained,
            }
        )
        encoded = _canonical_json_bytes(request.payload)
        if len(encoded) > self._max_artifact_bytes:
            raise ValueError("artifact payload exceeds the configured size limit")
        async with self._lock:
            return await asyncio.to_thread(self._put_sync, request, encoded)

    async def get_json(
        self,
        *,
        tenant_id: str,
        user_id: str,
        run_id: str,
        artifact_id: str,
    ) -> JsonValue:
        if _ARTIFACT_ID_PATTERN.fullmatch(artifact_id) is None:
            raise ArtifactStoreError(
                ErrorCode.AGENT_ARTIFACT_NOT_FOUND,
                "artifact was not found",
            )
        async with self._lock:
            return await asyncio.to_thread(
                self._get_sync,
                tenant_id,
                user_id,
                run_id,
                artifact_id,
            )

    async def get_safe_preview(
        self,
        *,
        tenant_id: str,
        user_id: str,
        run_id: str,
        artifact_id: str,
        max_rows: int = 20,
        max_columns: int = 20,
        max_string_chars: int = 240,
        max_depth: int = 5,
    ) -> JsonValue:
        if min(max_rows, max_columns, max_string_chars, max_depth) < 1:
            raise ValueError("safe preview limits must be positive")
        payload = await self.get_json(
            tenant_id=tenant_id,
            user_id=user_id,
            run_id=run_id,
            artifact_id=artifact_id,
        )
        return _safe_preview(
            payload,
            max_rows=max_rows,
            max_columns=max_columns,
            max_string_chars=max_string_chars,
            max_depth=max_depth,
        )

    async def list_for_run(
        self,
        *,
        tenant_id: str,
        user_id: str,
        run_id: str,
    ) -> tuple[AgentArtifactRef, ...]:
        async with self._lock:
            return await asyncio.to_thread(
                self._list_sync,
                tenant_id,
                user_id,
                run_id,
            )

    async def delete_expired(
        self,
        *,
        now: datetime | None = None,
    ) -> tuple[str, ...]:
        boundary = now or self._clock()
        if boundary.tzinfo is None or boundary.utcoffset() is None:
            raise ValueError("retention boundary must be timezone-aware")
        async with self._lock:
            return await asyncio.to_thread(self._delete_expired_sync, boundary)

    def _initialize(self) -> None:
        with closing(self._connect()) as connection, connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS agent_artifacts (
                    tenant_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    artifact_id TEXT NOT NULL,
                    call_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    digest TEXT NOT NULL,
                    schema_digest TEXT,
                    row_count INTEGER,
                    sensitivity TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    retained INTEGER NOT NULL,
                    relative_path TEXT NOT NULL,
                    PRIMARY KEY (tenant_id, user_id, run_id, artifact_id),
                    UNIQUE (tenant_id, user_id, run_id, call_id)
                );
                CREATE INDEX IF NOT EXISTS agent_artifacts_expiry
                ON agent_artifacts (expires_at, retained);
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._database_path, timeout=5)
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def _put_sync(
        self,
        request: _ArtifactWrite,
        encoded: bytes,
    ) -> AgentArtifactRef:
        digest = _sha256(encoded)
        identity = "\0".join(
            (
                request.tenant_id,
                request.user_id,
                request.run_id,
                request.call_id,
                request.kind.value,
                digest,
            )
        ).encode("utf-8")
        artifact_id = "artifact-" + _sha256(identity)
        created_at = self._clock()
        if created_at.tzinfo is None or created_at.utcoffset() is None:
            raise ValueError("Artifact Store clock must return a timezone-aware datetime")
        created_at = created_at.astimezone(UTC)
        expires_at = created_at + timedelta(seconds=request.retention_seconds)
        relative_path = Path(
            _owner_hash(request.tenant_id),
            _owner_hash(request.user_id),
            _owner_hash(request.run_id),
            f"{artifact_id}.json",
        )
        destination = self._safe_payload_path(relative_path)

        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT artifact_id, kind, digest, schema_digest, row_count,
                       sensitivity, created_at, relative_path
                FROM agent_artifacts
                WHERE tenant_id = ? AND user_id = ? AND run_id = ? AND call_id = ?
                """,
                (
                    request.tenant_id,
                    request.user_id,
                    request.run_id,
                    request.call_id,
                ),
            ).fetchone()
            if existing is not None:
                expected_metadata = (
                    request.kind.value,
                    digest,
                    request.schema_digest,
                    request.row_count,
                    request.sensitivity,
                )
                actual_metadata = (
                    str(existing[1]),
                    str(existing[2]),
                    None if existing[3] is None else str(existing[3]),
                    None if existing[4] is None else int(existing[4]),
                    str(existing[5]),
                )
                if actual_metadata != expected_metadata:
                    raise ArtifactStoreError(
                        ErrorCode.AGENT_ARTIFACT_INTEGRITY_ERROR,
                        "artifact call already completed with a different payload",
                    )
                self._read_and_verify(existing[7], digest)
                return self._row_to_ref(request.run_id, existing)

            destination.parent.mkdir(parents=True, exist_ok=True)
            handle = tempfile.NamedTemporaryFile(
                mode="wb",
                prefix=f".{artifact_id}-",
                suffix=".tmp",
                dir=destination.parent,
                delete=False,
            )
            temporary_path = Path(handle.name)
            replaced = False
            try:
                with handle:
                    handle.write(encoded)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.chmod(temporary_path, 0o600)
                self._atomic_replace(temporary_path, destination)
                replaced = True
                connection.execute(
                    """
                    INSERT INTO agent_artifacts (
                        tenant_id, user_id, run_id, artifact_id, call_id, kind,
                        digest, schema_digest, row_count, sensitivity, created_at,
                        expires_at, retained, relative_path
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        request.tenant_id,
                        request.user_id,
                        request.run_id,
                        artifact_id,
                        request.call_id,
                        request.kind.value,
                        digest,
                        request.schema_digest,
                        request.row_count,
                        request.sensitivity,
                        created_at.isoformat(),
                        expires_at.isoformat(),
                        int(request.retained),
                        relative_path.as_posix(),
                    ),
                )
            except BaseException:
                if temporary_path.exists():
                    temporary_path.unlink()
                if replaced and destination.exists():
                    destination.unlink()
                raise

        return AgentArtifactRef(
            artifact_id=artifact_id,
            run_id=request.run_id,
            kind=request.kind,
            digest=digest,
            schema_digest=request.schema_digest,
            row_count=request.row_count,
            sensitivity=request.sensitivity,
            created_at=created_at,
        )

    def _get_sync(
        self,
        tenant_id: str,
        user_id: str,
        run_id: str,
        artifact_id: str,
    ) -> JsonValue:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                """
                SELECT relative_path, digest
                FROM agent_artifacts
                WHERE tenant_id = ? AND user_id = ?
                  AND run_id = ? AND artifact_id = ?
                """,
                (tenant_id, user_id, run_id, artifact_id),
            ).fetchone()
        if row is None:
            raise ArtifactStoreError(
                ErrorCode.AGENT_ARTIFACT_NOT_FOUND,
                "artifact was not found",
            )
        encoded = self._read_and_verify(str(row[0]), str(row[1]))
        try:
            return json.loads(encoded)
        except json.JSONDecodeError as exc:
            raise ArtifactStoreError(
                ErrorCode.AGENT_ARTIFACT_INTEGRITY_ERROR,
                "artifact payload is not valid JSON",
            ) from exc

    def _list_sync(
        self,
        tenant_id: str,
        user_id: str,
        run_id: str,
    ) -> tuple[AgentArtifactRef, ...]:
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                """
                SELECT artifact_id, kind, digest, schema_digest, row_count,
                       sensitivity, created_at, relative_path
                FROM agent_artifacts
                WHERE tenant_id = ? AND user_id = ? AND run_id = ?
                ORDER BY created_at, artifact_id
                """,
                (tenant_id, user_id, run_id),
            ).fetchall()
        return tuple(self._row_to_ref(run_id, row) for row in rows)

    def _delete_expired_sync(self, now: datetime) -> tuple[str, ...]:
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT tenant_id, user_id, run_id, artifact_id, relative_path
                FROM agent_artifacts
                WHERE retained = 0 AND expires_at <= ?
                ORDER BY expires_at, artifact_id
                """,
                (now.astimezone(UTC).isoformat(),),
            ).fetchall()
            deleted: list[str] = []
            for tenant_id, user_id, run_id, artifact_id, relative_path in rows:
                path = self._safe_payload_path(str(relative_path))
                if path.exists():
                    path.unlink()
                connection.execute(
                    """
                    DELETE FROM agent_artifacts
                    WHERE tenant_id = ? AND user_id = ?
                      AND run_id = ? AND artifact_id = ?
                    """,
                    (tenant_id, user_id, run_id, artifact_id),
                )
                deleted.append(str(artifact_id))
        return tuple(deleted)

    def _safe_payload_path(self, relative_path: str | Path) -> Path:
        relative = Path(relative_path)
        if relative.is_absolute() or relative.suffix != ".json":
            raise ArtifactStoreError(
                ErrorCode.AGENT_ARTIFACT_INTEGRITY_ERROR,
                "artifact metadata contains an invalid payload path",
            )
        candidate = (self._payload_root / relative).resolve()
        if not candidate.is_relative_to(self._payload_root):
            raise ArtifactStoreError(
                ErrorCode.AGENT_ARTIFACT_INTEGRITY_ERROR,
                "artifact metadata contains an invalid payload path",
            )
        return candidate

    def _read_and_verify(self, relative_path: str | Path, digest: str) -> bytes:
        path = self._safe_payload_path(relative_path)
        try:
            size = path.stat().st_size
            if size > self._max_artifact_bytes:
                raise ArtifactStoreError(
                    ErrorCode.AGENT_ARTIFACT_INTEGRITY_ERROR,
                    "artifact payload exceeds the configured size limit",
                )
            encoded = path.read_bytes()
        except FileNotFoundError as exc:
            raise ArtifactStoreError(
                ErrorCode.AGENT_ARTIFACT_INTEGRITY_ERROR,
                "artifact payload is missing",
            ) from exc
        if _sha256(encoded) != digest:
            raise ArtifactStoreError(
                ErrorCode.AGENT_ARTIFACT_INTEGRITY_ERROR,
                "artifact payload integrity check failed",
            )
        return encoded

    @staticmethod
    def _row_to_ref(run_id: str, row: sqlite3.Row | tuple[object, ...]) -> AgentArtifactRef:
        return AgentArtifactRef(
            artifact_id=str(row[0]),
            run_id=run_id,
            kind=str(row[1]),
            digest=str(row[2]),
            schema_digest=None if row[3] is None else str(row[3]),
            row_count=None if row[4] is None else int(row[4]),
            sensitivity=str(row[5]),
            created_at=datetime.fromisoformat(str(row[6])),
        )


def _safe_preview(
    value: JsonValue,
    *,
    max_rows: int,
    max_columns: int,
    max_string_chars: int,
    max_depth: int,
    depth: int = 0,
    key: str | None = None,
) -> JsonValue:
    if key is not None and any(marker in key.lower() for marker in _SENSITIVE_KEY_MARKERS):
        return "[REDACTED]"
    if depth >= max_depth and isinstance(value, (dict, list)):
        return "[TRUNCATED]"
    if isinstance(value, str):
        return value if len(value) <= max_string_chars else value[:max_string_chars] + "…"
    if isinstance(value, list):
        return [
            _safe_preview(
                item,
                max_rows=max_rows,
                max_columns=max_columns,
                max_string_chars=max_string_chars,
                max_depth=max_depth,
                depth=depth + 1,
            )
            for item in value[:max_rows]
        ]
    if isinstance(value, dict):
        return {
            item_key: _safe_preview(
                item,
                max_rows=max_rows,
                max_columns=max_columns,
                max_string_chars=max_string_chars,
                max_depth=max_depth,
                depth=depth + 1,
                key=item_key,
            )
            for item_key, item in list(value.items())[:max_columns]
        }
    return value


__all__ = ["ArtifactStoreError", "SQLiteArtifactStore"]
