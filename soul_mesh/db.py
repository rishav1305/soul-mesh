"""Thin async SQLite wrapper for mesh operations.

Replaces brain.db.store with a standalone module.
Uses aiosqlite with connect-per-call (matches node.py pattern).
"""

from __future__ import annotations

import aiosqlite

# Table allowlist for insert() -- prevents SQL injection via table name
_INSERTABLE_TABLES: frozenset[str] = frozenset({
    "mesh_nodes", "pending_writes",
    "events", "tasks", "knowledge", "chat_history",
})


class MeshDB:
    """Async SQLite wrapper for mesh node storage."""

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._memory_db: aiosqlite.Connection | None = None

    async def _connect(self) -> aiosqlite.Connection:
        if self._db_path == ":memory:":
            if self._memory_db is None:
                self._memory_db = await aiosqlite.connect(":memory:")
                self._memory_db.row_factory = aiosqlite.Row
            return self._memory_db
        conn = await aiosqlite.connect(self._db_path)
        conn.row_factory = aiosqlite.Row
        return conn

    async def _close(self, conn: aiosqlite.Connection) -> None:
        if self._db_path != ":memory:":
            await conn.close()

    async def fetch_all(
        self, sql: str, params: tuple = ()
    ) -> list[dict]:
        conn = await self._connect()
        try:
            cursor = await conn.execute(sql, params)
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]
        finally:
            await self._close(conn)

    async def fetch_one(
        self, sql: str, params: tuple = ()
    ) -> dict | None:
        conn = await self._connect()
        try:
            cursor = await conn.execute(sql, params)
            row = await cursor.fetchone()
            return dict(row) if row else None
        finally:
            await self._close(conn)

    async def execute(self, sql: str, params: tuple = ()) -> None:
        conn = await self._connect()
        try:
            await conn.execute(sql, params)
            await conn.commit()
        finally:
            await self._close(conn)

    async def insert(self, table: str, data: dict) -> int:
        """Insert a row and return the rowid."""
        if table not in _INSERTABLE_TABLES:
            raise ValueError(f"Table {table!r} not in insertable allowlist")
        cols = list(data.keys())
        placeholders = ", ".join("?" for _ in cols)
        col_names = ", ".join(cols)
        sql = f"INSERT INTO {table} ({col_names}) VALUES ({placeholders})"
        conn = await self._connect()
        try:
            cursor = await conn.execute(sql, tuple(data.values()))
            await conn.commit()
            return cursor.lastrowid
        finally:
            await self._close(conn)

    async def ensure_tables(self) -> None:
        """Create mesh tables if they don't exist."""
        conn = await self._connect()
        try:
            await conn.execute(
                """CREATE TABLE IF NOT EXISTS mesh_nodes (
                    id TEXT PRIMARY KEY,
                    account_id TEXT DEFAULT '',
                    name TEXT DEFAULT '',
                    host TEXT DEFAULT '',
                    port INTEGER DEFAULT 8340,
                    platform TEXT DEFAULT '',
                    arch TEXT DEFAULT '',
                    ram_mb INTEGER DEFAULT 0,
                    storage_mb INTEGER DEFAULT 0,
                    is_hub INTEGER DEFAULT 0,
                    capability REAL DEFAULT 0.0,
                    last_seen TEXT DEFAULT '',
                    status TEXT DEFAULT 'online'
                )"""
            )
            await conn.execute(
                """CREATE TABLE IF NOT EXISTS pending_writes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    target_table TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    status TEXT DEFAULT 'pending',
                    retry_count INTEGER DEFAULT 0
                )"""
            )
            await conn.commit()
        finally:
            await self._close(conn)
