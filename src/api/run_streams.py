"""Persistent run-event replay and tenant-scoped cancellation coordination."""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import AsyncIterator, Callable
from contextlib import closing
from pathlib import Path
from typing import TypeVar

from data_agent.runtime.events import AgentEvent, AgentEventType
from data_agent.runtime.models import PrincipalContext


_T = TypeVar("_T")
_RUNNING = "running"
_WAITING = "waiting"
_COMPLETED = "completed"
_FAILED = "failed"
_CANCELLED = "cancelled"
_CLOSED_STATUSES = frozenset(
    {
        _COMPLETED,
        _FAILED,
        _CANCELLED,
        # Read compatibility for Task 2 databases.
        AgentEventType.RUN_COMPLETED.value,
        AgentEventType.RUN_FAILED.value,
    }
)


class RunConflictError(RuntimeError):
    pass


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
        *,
        conversation_id: str | None = None,
    ) -> None:
        payload = event.model_dump_json()
        conversation_id = conversation_id.strip() if conversation_id else None

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
            run_row = connection.execute(
                """
                SELECT status, conversation_id,
                       (SELECT MAX(sequence) FROM run_events
                        WHERE tenant_id = ? AND user_id = ? AND run_id = ?)
                FROM runs
                WHERE tenant_id = ? AND user_id = ? AND run_id = ?
                """,
                (
                    principal.tenant_id,
                    principal.user_id,
                    event.run_id,
                    principal.tenant_id,
                    principal.user_id,
                    event.run_id,
                ),
            ).fetchone()
            if run_row is None:
                if event.sequence != 0 or event.type != AgentEventType.RUN_STARTED:
                    raise ValueError("run event sequence must start at run_started sequence 0")
                if conversation_id is not None:
                    conflict = connection.execute(
                        """
                        SELECT run_id
                        FROM runs
                        WHERE tenant_id = ? AND user_id = ?
                          AND conversation_id = ?
                          AND status IN (?, ?)
                        LIMIT 1
                        """,
                        (
                            principal.tenant_id,
                            principal.user_id,
                            conversation_id,
                            _RUNNING,
                            _WAITING,
                        ),
                    ).fetchone()
                    if conflict is not None:
                        raise RunConflictError(
                            "conversation already has an active or waiting run"
                        )
                current_status = None
            else:
                current_status = str(run_row[0])
                stored_conversation_id = run_row[1]
                maximum_sequence = int(run_row[2])
                if (
                    conversation_id is not None
                    and stored_conversation_id is not None
                    and conversation_id != stored_conversation_id
                ):
                    raise ValueError("run conversation ownership cannot change")
                if current_status in _CLOSED_STATUSES:
                    raise ValueError("terminal run cannot accept more events")
                if event.sequence != maximum_sequence + 1:
                    raise ValueError("run event sequence must be monotonic and contiguous")
                if current_status == "waiting" and event.type not in {
                    AgentEventType.RUN_RESUMED,
                    AgentEventType.RUN_FAILED,
                }:
                    raise ValueError("waiting run requires resumed or failed event")
                if (
                    event.type == AgentEventType.RUN_RESUMED
                    and current_status != "waiting"
                ):
                    raise ValueError("only a waiting run can be resumed")

            if event.type == AgentEventType.RUN_FAILED:
                status = (
                    _CANCELLED
                    if event.data.error_code.value == "CANCELLED"
                    else _FAILED
                )
            else:
                status = {
                    AgentEventType.RUN_WAITING: _WAITING,
                    AgentEventType.RUN_RESUMED: _RUNNING,
                    AgentEventType.RUN_COMPLETED: _COMPLETED,
                }.get(event.type, current_status or _RUNNING)
            connection.execute(
                """
                INSERT INTO runs (
                    tenant_id, user_id, run_id, conversation_id, status
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (tenant_id, user_id, run_id)
                DO UPDATE SET
                    conversation_id = COALESCE(
                        runs.conversation_id,
                        excluded.conversation_id
                    ),
                    status = excluded.status
                """,
                (
                    principal.tenant_id,
                    principal.user_id,
                    event.run_id,
                    conversation_id,
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

    async def status(
        self,
        *,
        tenant_id: str,
        user_id: str,
        run_id: str,
    ) -> str | None:
        def operation(connection: sqlite3.Connection) -> str | None:
            row = connection.execute(
                """
                SELECT status
                FROM runs
                WHERE tenant_id = ? AND user_id = ? AND run_id = ?
                """,
                (tenant_id, user_id, run_id),
            ).fetchone()
            return None if row is None else str(row[0])

        return await self._read(operation)

    async def last_sequence(
        self,
        *,
        tenant_id: str,
        user_id: str,
        run_id: str,
    ) -> int | None:
        def operation(connection: sqlite3.Connection) -> int | None:
            row = connection.execute(
                """
                SELECT MAX(sequence)
                FROM run_events
                WHERE tenant_id = ? AND user_id = ? AND run_id = ?
                """,
                (tenant_id, user_id, run_id),
            ).fetchone()
            return None if row is None or row[0] is None else int(row[0])

        return await self._read(operation)

    async def ensure_conversation_available(
        self,
        *,
        tenant_id: str,
        user_id: str,
        conversation_id: str,
    ) -> None:
        def operation(connection: sqlite3.Connection) -> None:
            row = connection.execute(
                """
                SELECT run_id
                FROM runs
                WHERE tenant_id = ? AND user_id = ?
                  AND conversation_id = ?
                  AND status IN (?, ?)
                LIMIT 1
                """,
                (tenant_id, user_id, conversation_id, _RUNNING, _WAITING),
            ).fetchone()
            if row is not None:
                raise RunConflictError(
                    "conversation already has an active or waiting run"
                )

        await self._read(operation)

    def _initialize(self) -> None:
        with closing(self._connect()) as connection, connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    tenant_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    conversation_id TEXT,
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
            columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(runs)").fetchall()
            }
            if "conversation_id" not in columns:
                connection.execute(
                    "ALTER TABLE runs ADD COLUMN conversation_id TEXT"
                )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_runs_active_conversation
                ON runs (tenant_id, user_id, conversation_id, status)
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
        with closing(self._connect()) as connection, connection:
            return operation(connection)

    def _run_write(
        self,
        operation: Callable[[sqlite3.Connection], _T],
    ) -> _T:
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            return operation(connection)


class RunCoordinator:
    """Own active stream tasks and persist every emitted event."""

    def __init__(self, store: RunEventStore) -> None:
        self._store = store
        self._active: dict[
            tuple[str, str, str], asyncio.Task[object]
        ] = {}
        self._conversation_reservations: dict[
            tuple[str, str, str], asyncio.Task[object]
        ] = {}
        self._lock = asyncio.Lock()

    async def ensure_conversation_available(
        self,
        *,
        tenant_id: str,
        user_id: str,
        conversation_id: str | None,
    ) -> None:
        if conversation_id is None:
            return
        normalized = conversation_id.strip()
        key = (tenant_id, user_id, normalized)
        async with self._lock:
            if key in self._conversation_reservations:
                raise RunConflictError(
                    "conversation already has an active or waiting run"
                )
            await self._store.ensure_conversation_available(
                tenant_id=tenant_id,
                user_id=user_id,
                conversation_id=normalized,
            )

    async def observe(
        self,
        principal: PrincipalContext,
        events: AsyncIterator[AgentEvent],
        *,
        conversation_id: str | None = None,
        existing_run: bool = False,
    ) -> AsyncIterator[AgentEvent]:
        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("run stream requires an active asyncio task")
        key: tuple[str, str, str] | None = None
        conversation_key: tuple[str, str, str] | None = None
        if conversation_id is not None and not existing_run:
            normalized = conversation_id.strip()
            conversation_key = (
                principal.tenant_id,
                principal.user_id,
                normalized,
            )
            async with self._lock:
                if conversation_key in self._conversation_reservations:
                    raise RunConflictError(
                        "conversation already has an active or waiting run"
                    )
                await self._store.ensure_conversation_available(
                    tenant_id=principal.tenant_id,
                    user_id=principal.user_id,
                    conversation_id=normalized,
                )
                self._conversation_reservations[conversation_key] = task
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
                await self._store.append(
                    principal,
                    event,
                    conversation_id=(
                        conversation_id if event.sequence == 0 else None
                    ),
                )
                yield event
        finally:
            if key is not None:
                async with self._lock:
                    if self._active.get(key) is task:
                        self._active.pop(key, None)
            if conversation_key is not None:
                async with self._lock:
                    if self._conversation_reservations.get(conversation_key) is task:
                        self._conversation_reservations.pop(conversation_key, None)

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
        if task is not asyncio.current_task():
            await asyncio.gather(task, return_exceptions=True)
        return True

    async def append(
        self,
        principal: PrincipalContext,
        event: AgentEvent,
    ) -> None:
        await self._store.append(principal, event)

    async def status(
        self,
        *,
        tenant_id: str,
        user_id: str,
        run_id: str,
    ) -> str | None:
        return await self._store.status(
            tenant_id=tenant_id,
            user_id=user_id,
            run_id=run_id,
        )

    async def next_sequence(
        self,
        *,
        tenant_id: str,
        user_id: str,
        run_id: str,
    ) -> int | None:
        value = await self._store.last_sequence(
            tenant_id=tenant_id,
            user_id=user_id,
            run_id=run_id,
        )
        return None if value is None else value + 1

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


__all__ = ["RunConflictError", "RunCoordinator", "RunEventStore"]
