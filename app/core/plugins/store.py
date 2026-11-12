"""SQLite state store for the independent Core runtime."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .contracts import ModuleStatus


@dataclass(frozen=True, slots=True)
class StoredModule:
    module_id: str
    version: int
    status: ModuleStatus
    last_error: str | None
    updated_at: str


class SqliteModuleStore:
    """Persisted Core state.

    Read-only instances deliberately never create a directory, database or schema.
    That keeps inspection commands safe while a Runtime owns the live database.
    """

    def __init__(self, path: Path, *, read_only: bool = False) -> None:
        self.path = path.expanduser().resolve(strict=False)
        self.read_only = read_only
        self.connection: sqlite3.Connection | None
        if read_only:
            if not self.path.exists():
                self.connection = None
                return
            self.connection = sqlite3.connect(f"{self.path.as_uri()}?mode=ro", uri=True)
            self.connection.row_factory = sqlite3.Row
            self.connection.execute("PRAGMA query_only = ON")
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS core_modules (
                module_id TEXT PRIMARY KEY,
                version INTEGER NOT NULL,
                status TEXT NOT NULL,
                last_error TEXT,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS core_module_operations (
                id INTEGER PRIMARY KEY,
                module_id TEXT NOT NULL,
                operation TEXT NOT NULL,
                status TEXT NOT NULL,
                detail TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS core_module_data (
                module_id TEXT NOT NULL,
                key TEXT NOT NULL,
                value TEXT NOT NULL,
                PRIMARY KEY (module_id, key)
            );
            """
        )
        self.connection.commit()

    def close(self) -> None:
        if self.connection is not None:
            self.connection.close()

    def _writable_connection(self) -> sqlite3.Connection:
        if self.read_only:
            raise RuntimeError("Core state store is read-only")
        assert self.connection is not None
        return self.connection

    def state(self, module_id: str) -> StoredModule | None:
        if self.connection is None:
            return None
        row = self.connection.execute("SELECT * FROM core_modules WHERE module_id = ?", (module_id,)).fetchone()
        if row is None:
            return None
        return StoredModule(
            row["module_id"], row["version"], ModuleStatus(row["status"]), row["last_error"], row["updated_at"]
        )

    def states(self) -> list[StoredModule]:
        if self.connection is None:
            return []
        rows = self.connection.execute("SELECT * FROM core_modules ORDER BY module_id").fetchall()
        return [
            StoredModule(
                row["module_id"], row["version"], ModuleStatus(row["status"]), row["last_error"], row["updated_at"]
            )
            for row in rows
        ]

    def set_state(self, module_id: str, version: int, status: ModuleStatus, error: str | None = None) -> None:
        now = datetime.now(UTC).isoformat()
        connection = self._writable_connection()
        connection.execute(
            """INSERT INTO core_modules (module_id, version, status, last_error, updated_at) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(module_id) DO UPDATE SET version=excluded.version, status=excluded.status,
            last_error=excluded.last_error, updated_at=excluded.updated_at""",
            (module_id, version, status.value, error, now),
        )
        connection.commit()

    def operation(self, module_id: str, operation: str, status: str, detail: str | None = None) -> None:
        connection = self._writable_connection()
        connection.execute(
            "INSERT INTO core_module_operations "
            "(module_id, operation, status, detail, created_at) VALUES (?, ?, ?, ?, ?)",
            (module_id, operation, status, detail, datetime.now(UTC).isoformat()),
        )
        connection.commit()

    def get(self, module_id: str, key: str) -> str | None:
        if self.connection is None:
            return None
        row = self.connection.execute(
            "SELECT value FROM core_module_data WHERE module_id = ? AND key = ?", (module_id, key)
        ).fetchone()
        return None if row is None else str(row["value"])

    def set(self, module_id: str, key: str, value: str) -> None:
        connection = self._writable_connection()
        connection.execute(
            "INSERT INTO core_module_data (module_id, key, value) VALUES (?, ?, ?) "
            "ON CONFLICT(module_id, key) DO UPDATE SET value=excluded.value",
            (module_id, key, value),
        )
        connection.commit()

    def delete_data(self, module_id: str) -> None:
        connection = self._writable_connection()
        connection.execute("DELETE FROM core_module_data WHERE module_id = ?", (module_id,))
        connection.commit()
