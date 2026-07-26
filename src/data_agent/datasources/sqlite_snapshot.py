"""Safe ingestion of uploaded SQLite files as immutable snapshots."""

from __future__ import annotations

import hashlib
import re
import shutil
import sqlite3
import stat
from pathlib import Path

from data_agent.tools.schemas import (
    CatalogColumn,
    CatalogRelation,
    CatalogSnapshot,
)

from .file_snapshot import FileSnapshotError, FileSnapshotErrorCode
from .models import DataSourceModel, NonBlankText


_IDENTIFIER = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


class SQLiteSnapshotResult(DataSourceModel):
    database_path: Path
    fingerprint: NonBlankText
    catalog: CatalogSnapshot


class SQLiteSnapshotImporter:
    def __init__(self, *, max_file_bytes: int = 256 * 1024 * 1024) -> None:
        self._max_file_bytes = max_file_bytes

    @staticmethod
    def _fingerprint(path: Path) -> str:
        digest = hashlib.sha256(b"sqlite-snapshot-v1\0")
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return "sha256:" + digest.hexdigest()

    def import_file(
        self,
        source: str | Path,
        *,
        output_directory: str | Path,
        source_id: str,
        version: int,
    ) -> SQLiteSnapshotResult:
        try:
            path = Path(source).expanduser().resolve(strict=True)
        except FileNotFoundError as exc:
            raise FileSnapshotError(
                FileSnapshotErrorCode.FILE_NOT_FOUND,
                "SQLite upload was not found",
            ) from exc
        if not path.is_file() or path.stat().st_size > self._max_file_bytes:
            raise FileSnapshotError(
                FileSnapshotErrorCode.SIZE_LIMIT_EXCEEDED,
                "SQLite upload exceeds the file size limit",
            )
        with path.open("rb") as stream:
            if stream.read(16) != b"SQLite format 3\x00":
                raise FileSnapshotError(
                    FileSnapshotErrorCode.UNSUPPORTED_FORMAT,
                    "upload is not a SQLite 3 database",
                )
        fingerprint = self._fingerprint(path)
        output_root = Path(output_directory).expanduser().resolve()
        output_root.mkdir(parents=True, exist_ok=True)
        safe_source = re.sub(r"[^a-zA-Z0-9_-]+", "_", source_id).strip("_")
        database_path = (
            output_root
            / f"{safe_source or 'sqlite'}-v{version}-{fingerprint[7:23]}.sqlite"
        )
        if database_path.exists():
            raise FileSnapshotError(
                FileSnapshotErrorCode.SNAPSHOT_EXISTS,
                "immutable SQLite snapshot already exists",
            )

        uri = path.as_uri() + "?mode=ro&immutable=1"
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(uri, uri=True)
            connection.execute("PRAGMA query_only = ON")
            relations: list[CatalogRelation] = []
            rows = connection.execute(
                """
                SELECT name
                FROM sqlite_master
                WHERE type IN ('table', 'view')
                  AND name NOT LIKE 'sqlite_%'
                ORDER BY name
                """
            ).fetchall()
            for (table_name,) in rows:
                name = str(table_name)
                if not _IDENTIFIER.fullmatch(name):
                    raise FileSnapshotError(
                        FileSnapshotErrorCode.IMPORT_FAILED,
                        "SQLite table names must use letters, numbers, and underscores",
                    )
                escaped = name.replace('"', '""')
                columns = connection.execute(
                    f'PRAGMA main.table_info("{escaped}")'
                ).fetchall()
                if not columns:
                    continue
                relations.append(
                    CatalogRelation(
                        relation=f"main.{name}",
                        columns=tuple(
                            CatalogColumn(
                                name=str(column[1]),
                                data_type=str(column[2] or "unknown"),
                                nullable=not bool(column[3]),
                            )
                            for column in columns
                        ),
                    )
                )
            if not relations:
                raise FileSnapshotError(
                    FileSnapshotErrorCode.EMPTY_DATASET,
                    "SQLite upload contains no importable tables",
                )
            connection.close()
            connection = None
            shutil.copyfile(path, database_path)
            database_path.chmod(stat.S_IRUSR | stat.S_IRGRP)
            catalog = CatalogSnapshot(
                schema_fingerprint=fingerprint,
                relations=tuple(relations),
            )
            return SQLiteSnapshotResult(
                database_path=database_path,
                fingerprint=fingerprint,
                catalog=catalog,
            )
        except FileSnapshotError:
            raise
        except (OSError, sqlite3.DatabaseError) as exc:
            raise FileSnapshotError(
                FileSnapshotErrorCode.IMPORT_FAILED,
                "SQLite upload could not be imported safely",
            ) from exc
        finally:
            if connection is not None:
                connection.close()
            if database_path.exists() and database_path.stat().st_size == 0:
                database_path.unlink(missing_ok=True)


__all__ = ["SQLiteSnapshotImporter", "SQLiteSnapshotResult"]
