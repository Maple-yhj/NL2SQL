"""Explicit-lifecycle, non-pickle checkpointer factories."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer


_CHECKPOINT_TYPE_MODULES = (
    "data_agent.analysis_agent.models",
    "data_agent.public_contracts",
    "data_agent.runtime.models",
)


def _allowed_checkpoint_types() -> tuple[tuple[str, str], ...]:
    """Allow only contract types defined by the checkpoint state modules."""

    from importlib import import_module

    allowed: list[tuple[str, str]] = []
    for module_name in _CHECKPOINT_TYPE_MODULES:
        module = import_module(module_name)
        allowed.extend(
            (module_name, name)
            for name, value in vars(module).items()
            if isinstance(value, type) and value.__module__ == module_name
        )
    return tuple(sorted(allowed))


def checkpoint_serializer() -> JsonPlusSerializer:
    return JsonPlusSerializer(
        pickle_fallback=False,
        allowed_msgpack_modules=_allowed_checkpoint_types(),
    )


@dataclass(slots=True)
class CheckpointerResource:
    checkpointer: Any
    _close: Any = None
    _closed: bool = False

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._close is not None:
            await self._close()


class CheckpointerFactory(Protocol):
    async def open(self) -> CheckpointerResource: ...


class InMemoryCheckpointerFactory:
    async def open(self) -> CheckpointerResource:
        return CheckpointerResource(
            InMemorySaver(serde=checkpoint_serializer())
        )


class SQLiteCheckpointerFactory:
    def __init__(self, state_root: str | Path) -> None:
        self._state_root = Path(state_root).expanduser().resolve()
        self.database_path = self._state_root / "control" / "agent-checkpoints.sqlite3"

    async def open(self) -> CheckpointerResource:
        import aiosqlite
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = await aiosqlite.connect(self.database_path)
        try:
            checkpointer = AsyncSqliteSaver(
                connection,
                serde=checkpoint_serializer(),
            )
            await checkpointer.setup()
            self.database_path.chmod(0o600)
            return CheckpointerResource(checkpointer, connection.close)
        except BaseException:
            await connection.close()
            raise


class PostgresCheckpointerFactory:
    """Optional production factory; importing this module does not connect."""

    def __init__(self, connection_string: str) -> None:
        if not connection_string.strip():
            raise ValueError("PostgreSQL checkpointer requires a connection string")
        self._connection_string = connection_string

    async def open(self) -> CheckpointerResource:
        try:
            from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
        except ImportError as exc:  # pragma: no cover - optional production extra
            raise RuntimeError(
                "PostgreSQL checkpoint support is not installed"
            ) from exc
        manager = AsyncPostgresSaver.from_conn_string(self._connection_string)
        checkpointer = await manager.__aenter__()
        try:
            await checkpointer.setup()

            async def close() -> None:
                await manager.__aexit__(None, None, None)

            return CheckpointerResource(checkpointer, close)
        except BaseException:
            await manager.__aexit__(None, None, None)
            raise


__all__ = [
    "CheckpointerFactory",
    "CheckpointerResource",
    "InMemoryCheckpointerFactory",
    "PostgresCheckpointerFactory",
    "SQLiteCheckpointerFactory",
    "checkpoint_serializer",
]
