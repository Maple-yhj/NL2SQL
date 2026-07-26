"""Persistent run-event replay and tenant-scoped cancellation coordination."""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import TypeVar

from data_agent.runtime.events import AgentEvent, AgentEventType
from data_agent.runtime.models import PrincipalContext


_T = TypeVar("_T")


class RunEventStore:
    """Append-only SQLite event log scoped by tenant and user."""

    def __init__(self, database_path: str | Path) -> None:
        self._database_path = Path(database_path).expanduser().resolve()
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()
        self._initialize()

    async def append(
        self,
        principal: PrincipalContext,
        event: AgentEvent,
    ) -> None:
        payload = event.model_dump_json()

        def operation(connection: sqlite3.Connection) -> None:
            current = connection.execute(
                """
                SELECT payload
                FROM run_events
                WHERE tenant_id = ? AND user_id = ?
                  AND run_id = ? AND sequence = ?
                """,
                (
                    principal.tenant_id,
                    principal.user_id,
                    event.run_id,
                    event.sequence,
                ),
            ).fetchone()
            if current is not None:
                if str(current[0]) != payload:
                    raise ValueError(
                        "run event sequence already contains another payload"
                    )
                return
            status = (
                event.type.value
                if event.type
                in {
                    AgentEventType.RUN_COMPLETED,
                    AgentEventType.RUN_FAILED,
                }
                else "running"
            )
            connection.execute(
                """
                INSERT INTO runs (
                    tenant_id, user_id, run_id, status
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT (tenant_id, user_id, run_id)
                DO UPDATE SET status = excluded.status
                """,
                (
                    principal.tenant_id,
                    principal.user_id,
                    event.run_id,
                    status,
                ),
            )
            connection.execute(
                """
                INSERT INTO run_events (
                    tenant_id, user_id, run_id, sequence, payload
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    principal.tenant_id,
                    principal.user_id,
                    event.run_id,
                    event.sequence,
                    payload,
                ),
            )

        await self._write(operation)

    async def replay(
        self,
        *,
        tenant_id: str,
        user_id: str,
        run_id: str,
        after_sequence: int = -1,
        limit: int = 200,
    ) -> tuple[AgentEvent, ...]:
        def operation(
            connection: sqlite3.Connection,
        ) -> tuple[AgentEvent, ...]:
            rows = connection.execute(
                """
                SELECT payload
                FROM run_events
                WHERE tenant_id = ? AND user_id = ? AND run_id = ?
                  AND sequence > ?
                ORDER BY sequence
                LIMIT ?
                """,
                (
                    tenant_id,
                    user_id,
                    run_id,
                    after_sequence,
                    limit,
                ),
            ).fetchall()
            return tuple(
                AgentEvent.model_validate_json(row[0]) for row in rows
            )

        return await self._read(operation)

    async def contains(
        self,
        *,
        tenant_id: str,
        user_id: str,
        run_id: str,
    ) -> bool:
        def operation(connection: sqlite3.Connection) -> bool:
            row = connection.execute(
                """
                SELECT 1
                FROM runs
                WHERE tenant_id = ? AND user_id = ? AND run_id = ?
                """,
                (tenant_id, user_id, run_id),
            ).fetchone()
            return row is not None

        return await self._read(operation)

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    tenant_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    PRIMARY KEY (tenant_id, user_id, run_id)
                );
                CREATE TABLE IF NOT EXISTS run_events (
                    tenant_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    payload TEXT NOT NULL,
                    PRIMARY KEY (tenant_id, user_id, run_id, sequence),
                    FOREIGN KEY (tenant_id, user_id, run_id)
                        REFERENCES runs (tenant_id, user_id, run_id)
                        ON DELETE CASCADE
                );
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._database_path, timeout=5)
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    async def _read(
        self,
        operation: Callable[[sqlite3.Connection], _T],
    ) -> _T:
        async with self._lock:
            return await asyncio.to_thread(self._run_read, operation)

    async def _write(
        self,
        operation: Callable[[sqlite3.Connection], _T],
    ) -> _T:
        async with self._lock:
            return await asyncio.to_thread(self._run_write, operation)

    def _run_read(
        self,
        operation: Callable[[sqlite3.Connection], _T],
    ) -> _T:
        with self._connect() as connection:
            return operation(connection)

    def _run_write(
        self,
        operation: Callable[[sqlite3.Connection], _T],
    ) -> _T:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            return operation(connection)


class RunCoordinator:
    """Own active stream tasks and persist every emitted event."""

    def __init__(self, store: RunEventStore) -> None:
        self._store = store
        self._active: dict[
            tuple[str, str, str], asyncio.Task[object]
        ] = {}
        self._lock = asyncio.Lock()

    async def observe(
        self,
        principal: PrincipalContext,
        events: AsyncIterator[AgentEvent],
    ) -> AsyncIterator[AgentEvent]:
        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("run stream requires an active asyncio task")
        key: tuple[str, str, str] | None = None
        try:
            async for event in events:
                candidate = (
                    principal.tenant_id,
                    principal.user_id,
                    event.run_id,
                )
                if key is None:
                    key = candidate
                    async with self._lock:
                        if key in self._active:
                            raise RuntimeError("run is already being streamed")
                        self._active[key] = task
                elif candidate != key:
                    raise RuntimeError("runtime changed run_id within one stream")
                await self._store.append(principal, event)
                yield event
        finally:
            if key is not None:
                async with self._lock:
                    if self._active.get(key) is task:
                        self._active.pop(key, None)

    async def cancel(
        self,
        *,
        tenant_id: str,
        user_id: str,
        run_id: str,
    ) -> bool:
        async with self._lock:
            task = self._active.get((tenant_id, user_id, run_id))
            if task is None or task.done():
                return False
            task.cancel()
            return True

    async def replay(
        self,
        *,
        tenant_id: str,
        user_id: str,
        run_id: str,
        after_sequence: int = -1,
        limit: int = 200,
    ) -> tuple[AgentEvent, ...]:
        return await self._store.replay(
            tenant_id=tenant_id,
            user_id=user_id,
            run_id=run_id,
            after_sequence=after_sequence,
            limit=limit,
        )

    async def contains(
        self,
        *,
        tenant_id: str,
        user_id: str,
        run_id: str,
    ) -> bool:
        return await self._store.contains(
            tenant_id=tenant_id,
            user_id=user_id,
            run_id=run_id,
        )


__all__ = ["RunCoordinator", "RunEventStore"]
