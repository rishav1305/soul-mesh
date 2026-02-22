"""Thin async SQLite wrapper for mesh operations.

Replaces brain.db.store with a standalone module.
Uses aiosqlite with connect-per-call (matches node.py pattern).
"""

from __future__ import annotations

import aiosqlite
from contextlib import asynccontextmanager

# Table allowlist for insert() -- prevents SQL injection via table name
_INSERTABLE_TABLES: frozenset[str] = frozenset({
    "nodes", "heartbeats", "settings", "link_codes", "link_attempts",
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

    @asynccontextmanager
    async def transaction(self):
        """Async context manager yielding a cursor within a transaction.

        Usage::
            async with db.transaction() as cursor:
                await cursor.execute("INSERT ...")
                row = await cursor.fetchone()

        Commits on success, rolls back on exception.
        """
        conn = await self._connect()
        try:
            await conn.execute("BEGIN")
            cursor = await conn.cursor()
            try:
                yield cursor
                await conn.commit()
            except Exception:
                await conn.rollback()
                raise
            finally:
                await cursor.close()
        finally:
            await self._close(conn)

    async def upsert_node(self, data: dict) -> None:
        """Insert or update a node by id.

        Uses INSERT ... ON CONFLICT(id) DO UPDATE for:
        name, host, port, platform, arch, cpu_cores, ram_total_mb,
        storage_total_gb, last_heartbeat.
        """
        sql = """
            INSERT INTO nodes (id, name, host, port, platform, arch,
                               cpu_cores, ram_total_mb, storage_total_gb,
                               last_heartbeat)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                name = excluded.name,
                host = excluded.host,
                port = excluded.port,
                platform = excluded.platform,
                arch = excluded.arch,
                cpu_cores = excluded.cpu_cores,
                ram_total_mb = excluded.ram_total_mb,
                storage_total_gb = excluded.storage_total_gb,
                last_heartbeat = excluded.last_heartbeat
        """
        params = (
            data["id"],
            data.get("name", ""),
            data.get("host", ""),
            data.get("port", 8340),
            data.get("platform", ""),
            data.get("arch", ""),
            data.get("cpu_cores", 0),
            data.get("ram_total_mb", 0),
            data.get("storage_total_gb", 0),
            data.get("last_heartbeat", ""),
        )
        conn = await self._connect()
        try:
            await conn.execute(sql, params)
            await conn.commit()
        finally:
            await self._close(conn)

    async def ensure_tables(self) -> None:
        """Create mesh tables if they don't exist."""
        conn = await self._connect()
        try:
            await conn.execute(
                """CREATE TABLE IF NOT EXISTS nodes (
                    id TEXT PRIMARY KEY,
                    account_id TEXT DEFAULT '',
                    name TEXT NOT NULL DEFAULT '',
                    host TEXT NOT NULL DEFAULT '',
                    port INTEGER DEFAULT 8340,
                    role TEXT DEFAULT 'agent',
                    platform TEXT DEFAULT '',
                    arch TEXT DEFAULT '',
                    cpu_cores INTEGER DEFAULT 0,
                    ram_total_mb INTEGER DEFAULT 0,
                    storage_total_gb REAL DEFAULT 0,
                    status TEXT DEFAULT 'offline',
                    last_heartbeat TEXT DEFAULT '',
                    joined_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
                )"""
            )
            await conn.execute(
                """CREATE TABLE IF NOT EXISTS heartbeats (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    node_id TEXT NOT NULL REFERENCES nodes(id),
                    cpu_usage_percent REAL DEFAULT 0,
                    cpu_load_1m REAL DEFAULT 0,
                    ram_available_mb INTEGER DEFAULT 0,
                    ram_used_percent REAL DEFAULT 0,
                    storage_free_gb REAL DEFAULT 0,
                    recorded_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
                )"""
            )
            await conn.execute(
                """CREATE TABLE IF NOT EXISTS link_codes (
                    code TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                )"""
            )
            await conn.execute(
                """CREATE TABLE IF NOT EXISTS link_attempts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ip_address TEXT NOT NULL,
                    code TEXT NOT NULL,
                    success INTEGER DEFAULT 0,
                    attempted_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
                )"""
            )
            await conn.execute(
                """CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )"""
            )
            await conn.commit()
        finally:
            await self._close(conn)
