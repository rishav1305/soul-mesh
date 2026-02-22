# soul-mesh Sync Extraction Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Extract sync.py from soul-os with MeshDB extensions for insert() and pending_writes table.

**Architecture:** Extend MeshDB with insert() and pending_writes schema, then adapt MeshSync to use injected dependencies instead of brain.* globals.

**Tech Stack:** Python 3.11+, aiosqlite, structlog, pytest-asyncio

---

### Task 1: Extend MeshDB with insert() and pending_writes table

**Files:**
- Modify: `soul_mesh/db.py`
- Modify: `tests/test_db.py`

**Step 1: Add tests for insert() and pending_writes**

Add to tests/test_db.py:

```python
async def test_insert_returns_rowid(self, db):
    rowid = await db.insert("mesh_nodes", {
        "id": "node-1", "name": "test", "status": "online",
    })
    assert rowid is not None
    assert rowid > 0

async def test_insert_data_retrievable(self, db):
    await db.insert("mesh_nodes", {
        "id": "node-2", "name": "second", "status": "offline",
    })
    row = await db.fetch_one(
        "SELECT id, name, status FROM mesh_nodes WHERE id = ?", ("node-2",)
    )
    assert row["name"] == "second"
    assert row["status"] == "offline"

async def test_ensure_tables_creates_pending_writes(self, db):
    rows = await db.fetch_all(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='pending_writes'"
    )
    assert len(rows) == 1

async def test_insert_pending_write(self, db):
    rowid = await db.insert("pending_writes", {
        "target_table": "events",
        "payload": '{"table":"events","data":{}}',
        "status": "pending",
    })
    assert rowid > 0
    row = await db.fetch_one(
        "SELECT target_table, status, retry_count FROM pending_writes WHERE id = ?",
        (rowid,),
    )
    assert row["target_table"] == "events"
    assert row["status"] == "pending"
    assert row["retry_count"] == 0
```

**Step 2: Implement insert() and update ensure_tables()**

Add to MeshDB in db.py:

```python
# Table allowlist for insert() -- prevents SQL injection via table name
_INSERTABLE_TABLES: frozenset[str] = frozenset({
    "mesh_nodes", "pending_writes",
    "events", "tasks", "knowledge", "chat_history",
})

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
```

Add pending_writes table to ensure_tables():

```python
await conn.execute(
    """CREATE TABLE IF NOT EXISTS pending_writes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        target_table TEXT NOT NULL,
        payload TEXT NOT NULL,
        status TEXT DEFAULT 'pending',
        retry_count INTEGER DEFAULT 0
    )"""
)
```

**Step 3: Run tests**

Run: `python -m pytest tests/test_db.py -v`
Expected: All tests PASS (7 existing + 4 new = 11)

---

### Task 2: Create soul_mesh/sync.py with tests (TDD)

**Files:**
- Create: `soul_mesh/sync.py`
- Create: `tests/test_sync.py`

**Step 1: Write failing tests**

```python
# tests/test_sync.py
"""Tests for mesh sync -- hub-proxy writes with offline queue."""

from __future__ import annotations

import asyncio
import json

import pytest

from soul_mesh.sync import (
    MAX_PENDING_WRITES,
    MAX_RETRY,
    REPLAY_BATCH,
    MeshSync,
    _MESH_WRITABLE_COLUMNS,
    _MESH_WRITABLE_TABLES,
)


class MockNode:
    def __init__(self, node_id="local-1", account_id="acct-1", is_hub=False):
        self.id = node_id
        self.account_id = account_id
        self.is_hub = is_hub


class MockTransport:
    def __init__(self):
        self.handlers: dict = {}
        self.sent: list[tuple] = []
        self.fail_send = False

    def on(self, msg_type, handler):
        self.handlers[msg_type] = handler

    async def send_to_hub(self, msg_type, payload):
        if self.fail_send:
            raise ConnectionError("Hub unreachable")
        self.sent.append((msg_type, payload))


class MockDB:
    def __init__(self):
        self._rows: dict[str, list[dict]] = {"pending_writes": [], "mesh_nodes": []}
        self._next_id = 1

    async def insert(self, table, data):
        row = dict(data)
        row["id"] = self._next_id
        self._next_id += 1
        self._rows.setdefault(table, []).append(row)
        return row["id"]

    async def fetch_one(self, sql, params=()):
        if "pending_writes" in sql and "COUNT" in sql:
            pending = [r for r in self._rows.get("pending_writes", []) if r.get("status") == "pending"]
            return {"cnt": len(pending)}
        if "mesh_nodes" in sql and params:
            for r in self._rows.get("mesh_nodes", []):
                if r["id"] == params[0]:
                    return r
            return None
        return None

    async def fetch_all(self, sql, params=()):
        if "pending_writes" in sql:
            pending = [r for r in self._rows.get("pending_writes", []) if r.get("status") == "pending"]
            limit = params[0] if params else len(pending)
            return pending[:limit]
        return []

    async def execute(self, sql, params=()):
        if "UPDATE" in sql and "status" in sql:
            for row in self._rows.get("pending_writes", []):
                if params and row["id"] == params[-1]:
                    if "retry_count" in sql:
                        row["retry_count"] = params[0]
                    if "'sent'" in sql:
                        row["status"] = "sent"
                    elif "'failed'" in sql:
                        row["status"] = "failed"
        elif "DELETE" in sql:
            pending = self._rows.get("pending_writes", [])
            to_delete = [r for r in pending if r.get("status") == "pending"][:100]
            for r in to_delete:
                pending.remove(r)

    def add_node(self, node_id, account_id="acct-1"):
        self._rows["mesh_nodes"].append({"id": node_id, "account_id": account_id})


class TestConstants:
    def test_max_retry(self):
        assert MAX_RETRY == 10

    def test_replay_batch(self):
        assert REPLAY_BATCH == 50

    def test_max_pending_writes(self):
        assert MAX_PENDING_WRITES == 10_000

    def test_writable_tables(self):
        assert "events" in _MESH_WRITABLE_TABLES
        assert "tasks" in _MESH_WRITABLE_TABLES
        assert "knowledge" in _MESH_WRITABLE_TABLES
        assert "chat_history" in _MESH_WRITABLE_TABLES
        assert "mesh_nodes" not in _MESH_WRITABLE_TABLES


class TestMeshSyncInit:
    def test_constructor(self):
        node = MockNode()
        transport = MockTransport()
        db = MockDB()
        sync = MeshSync(node, transport, db)
        assert sync._sync_interval == 30

    def test_custom_interval(self):
        sync = MeshSync(MockNode(), MockTransport(), MockDB(), sync_interval=60)
        assert sync._sync_interval == 60


class TestHubWrite:
    async def test_hub_writes_locally(self):
        node = MockNode(is_hub=True)
        db = MockDB()
        sync = MeshSync(node, MockTransport(), db)
        rowid = await sync.write("events", {"source": "test", "event_type": "ping"})
        assert rowid is not None
        assert len(db._rows["events"]) == 1

    async def test_non_hub_forwards_to_hub(self):
        node = MockNode(is_hub=False)
        transport = MockTransport()
        sync = MeshSync(node, transport, MockDB())
        await sync.write("events", {"source": "test"})
        assert len(transport.sent) == 1
        assert transport.sent[0][0] == "mesh_write"

    async def test_non_hub_queues_when_hub_unreachable(self):
        node = MockNode(is_hub=False)
        transport = MockTransport()
        transport.fail_send = True
        db = MockDB()
        sync = MeshSync(node, transport, db)
        await sync.write("events", {"source": "test"})
        assert len(db._rows["pending_writes"]) == 1
        assert db._rows["pending_writes"][0]["status"] == "pending"


class TestRemoteWriteHandler:
    async def test_hub_applies_write(self):
        node = MockNode(is_hub=True)
        db = MockDB()
        db.add_node("peer-1", "acct-1")
        transport = MockTransport()
        sync = MeshSync(node, transport, db)
        await sync.start()

        result = await sync._handle_remote_write(
            {"table": "events", "data": {"source": "remote", "event_type": "test"}},
            "peer-1",
        )
        assert result["ok"] is True
        assert "row_id" in result

    async def test_non_hub_rejects_write(self):
        node = MockNode(is_hub=False)
        sync = MeshSync(node, MockTransport(), MockDB())
        result = await sync._handle_remote_write(
            {"table": "events", "data": {"source": "x"}}, "peer-1"
        )
        assert result["ok"] is False
        assert "not hub" in result["error"]

    async def test_rejects_restricted_table(self):
        node = MockNode(is_hub=True)
        db = MockDB()
        db.add_node("peer-1", "acct-1")
        sync = MeshSync(node, MockTransport(), db)
        result = await sync._handle_remote_write(
            {"table": "mesh_nodes", "data": {"id": "evil"}}, "peer-1"
        )
        assert result["ok"] is False
        assert "not allowed" in result["error"]

    async def test_rejects_cross_account(self):
        node = MockNode(is_hub=True, account_id="acct-1")
        db = MockDB()
        db.add_node("peer-2", "acct-OTHER")
        sync = MeshSync(node, MockTransport(), db)
        result = await sync._handle_remote_write(
            {"table": "events", "data": {"source": "x"}}, "peer-2"
        )
        assert result["ok"] is False
        assert "account mismatch" in result["error"]

    async def test_filters_columns(self):
        node = MockNode(is_hub=True)
        db = MockDB()
        db.add_node("peer-1", "acct-1")
        sync = MeshSync(node, MockTransport(), db)
        result = await sync._handle_remote_write(
            {"table": "events", "data": {"source": "ok", "evil_col": "inject", "event_type": "test"}},
            "peer-1",
        )
        assert result["ok"] is True
        inserted = db._rows["events"][-1]
        assert "evil_col" not in inserted
        assert inserted["source"] == "ok"

    async def test_rejects_empty_payload(self):
        node = MockNode(is_hub=True)
        sync = MeshSync(node, MockTransport(), MockDB())
        result = await sync._handle_remote_write(None, "peer-1")
        assert result["ok"] is False

    async def test_rejects_no_writable_columns(self):
        node = MockNode(is_hub=True)
        db = MockDB()
        db.add_node("peer-1", "acct-1")
        sync = MeshSync(node, MockTransport(), db)
        result = await sync._handle_remote_write(
            {"table": "events", "data": {"evil_only": "val"}}, "peer-1"
        )
        assert result["ok"] is False
        assert "no writable columns" in result["error"]


class TestWritableColumns:
    def test_events_columns(self):
        assert _MESH_WRITABLE_COLUMNS["events"] == frozenset(
            {"source", "event_type", "payload", "session_id"}
        )

    def test_tasks_columns(self):
        assert "title" in _MESH_WRITABLE_COLUMNS["tasks"]
        assert "status" in _MESH_WRITABLE_COLUMNS["tasks"]


class TestStart:
    async def test_start_registers_handler(self):
        transport = MockTransport()
        sync = MeshSync(MockNode(), transport, MockDB())
        await sync.start()
        assert "mesh_write" in transport.handlers

    async def test_stop_cancels_replay(self):
        sync = MeshSync(MockNode(), MockTransport(), MockDB())
        await sync.start()
        assert sync._replay_task is not None
        await sync.stop()
        assert sync._replay_task.cancelled() or sync._replay_task.done()
```

**Step 2: Implement sync.py**

```python
# soul_mesh/sync.py
"""Mesh sync -- hub-proxy writes with an offline pending queue.

All DB writes from non-hub nodes are forwarded to the hub. When the
hub is unreachable, writes are queued in pending_writes (SQLite-backed).
A periodic replay loop drains the queue once connectivity returns.
"""

from __future__ import annotations

import asyncio
import json

import structlog

from soul_mesh.db import MeshDB

logger = structlog.get_logger("soul-mesh.sync")

MAX_RETRY = 10
REPLAY_BATCH = 50
MAX_PENDING_WRITES = 10_000

_MESH_WRITABLE_TABLES: frozenset[str] = frozenset({
    "events", "tasks", "knowledge", "chat_history",
})

_MESH_WRITABLE_COLUMNS: dict[str, frozenset[str]] = {
    "events": frozenset({"source", "event_type", "payload", "session_id"}),
    "tasks": frozenset({"title", "description", "status", "priority", "metadata"}),
    "knowledge": frozenset({"category", "title", "content", "source", "metadata"}),
    "chat_history": frozenset({"role", "content", "session_id"}),
}


class MeshSync:
    """Hub-proxy write coordinator with offline queue."""

    def __init__(self, local_node, transport, db: MeshDB, sync_interval: int = 30) -> None:
        self._local = local_node
        self._transport = transport
        self._db = db
        self._sync_interval = sync_interval
        self._replay_task: asyncio.Task | None = None

    async def start(self) -> None:
        self._replay_task = asyncio.create_task(self._replay_loop())
        self._transport.on("mesh_write", self._handle_remote_write)

    async def stop(self) -> None:
        if self._replay_task:
            self._replay_task.cancel()
            try:
                await self._replay_task
            except asyncio.CancelledError:
                pass

    async def write(self, table: str, data: dict) -> int | None:
        """Write to DB -- locally if hub, else forward to hub."""
        if self._local.is_hub:
            return await self._db.insert(table, data)

        payload = {"table": table, "data": data}
        try:
            await self._transport.send_to_hub("mesh_write", payload)
            return None
        except Exception as exc:
            count_row = await self._db.fetch_one(
                "SELECT COUNT(*) AS cnt FROM pending_writes "
                "WHERE status = 'pending'"
            )
            depth = count_row["cnt"] if count_row else 0
            if depth >= MAX_PENDING_WRITES:
                await self._db.execute(
                    "DELETE FROM pending_writes WHERE id IN ("
                    "  SELECT id FROM pending_writes WHERE status = 'pending' "
                    "  ORDER BY id ASC LIMIT 100"
                    ")"
                )
                logger.warning("Pending write queue full — dropped 100 oldest", depth=depth)
            await self._db.insert("pending_writes", {
                "target_table": table,
                "payload": json.dumps(payload),
                "status": "pending",
            })
            logger.warning("Hub unreachable — write queued", depth=depth, error=str(exc))
            return None

    async def _handle_remote_write(self, payload: dict | None, peer_id: str) -> dict:
        """Hub receives and applies a write from a non-hub node."""
        if not self._local.is_hub:
            logger.warning("Received mesh_write but not hub", peer_id=peer_id[:8])
            return {"ok": False, "error": "not hub"}

        if not payload:
            return {"ok": False, "error": "empty payload"}

        table = payload.get("table", "")
        data = payload.get("data", {})
        if not table or not data:
            return {"ok": False, "error": "missing table or data"}

        if table not in _MESH_WRITABLE_TABLES:
            logger.warning("Rejected write to restricted table", table=table, peer_id=peer_id[:8])
            return {"ok": False, "error": "table not allowed"}

        peer_row = await self._db.fetch_one(
            "SELECT account_id FROM mesh_nodes WHERE id = ?", (peer_id,)
        )
        if not peer_row or peer_row["account_id"] != self._local.account_id:
            logger.warning("Rejected cross-account write", peer_id=peer_id[:8])
            return {"ok": False, "error": "account mismatch"}

        allowed_cols = _MESH_WRITABLE_COLUMNS.get(table, frozenset())
        filtered_data = {k: v for k, v in data.items() if k in allowed_cols}
        if not filtered_data:
            return {"ok": False, "error": "no writable columns in payload"}

        try:
            row_id = await self._db.insert(table, filtered_data)
            logger.debug("Applied mesh write", table=table, peer_id=peer_id[:8], row_id=row_id)
            return {"ok": True, "row_id": row_id}
        except Exception as exc:
            logger.error("Failed to apply mesh write", peer_id=peer_id[:8], error=str(exc), exc_info=True)
            return {"ok": False, "error": "internal error"}

    async def _replay_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self._sync_interval)

                if self._local.is_hub:
                    continue

                pending = await self._db.fetch_all(
                    "SELECT id, payload, retry_count FROM pending_writes "
                    "WHERE status = 'pending' ORDER BY id ASC LIMIT ?",
                    (REPLAY_BATCH,),
                )
                if not pending:
                    continue

                for row in pending:
                    try:
                        p = json.loads(row["payload"])
                        await self._transport.send_to_hub("mesh_write", p)
                        await self._db.execute(
                            "UPDATE pending_writes SET status = 'sent' WHERE id = ?",
                            (row["id"],),
                        )
                    except (ConnectionError, asyncio.TimeoutError):
                        new_count = row["retry_count"] + 1
                        if new_count >= MAX_RETRY:
                            await self._db.execute(
                                "UPDATE pending_writes SET status = 'failed', retry_count = ? WHERE id = ?",
                                (new_count, row["id"]),
                            )
                            logger.error("Pending write hit max retries", write_id=row["id"])
                        else:
                            await self._db.execute(
                                "UPDATE pending_writes SET retry_count = ? WHERE id = ?",
                                (new_count, row["id"]),
                            )
                        break
                    except Exception as exc:
                        logger.error("Replay error", write_id=row["id"], error=str(exc))
                        break

            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.error("Replay loop error", error=str(exc))
```

**Step 3: Run tests**

Run: `python -m pytest tests/test_sync.py -v`
Expected: All tests PASS

---

### Task 3: Update __init__.py and verify full suite

**Files:**
- Modify: `soul_mesh/__init__.py`

**Step 1: Add sync exports**

```python
from soul_mesh.sync import MeshSync
```

**Step 2: Run full test suite**

Run: `python -m pytest tests/ -v`
Expected: All tests pass (52 existing + ~15 new)

**Step 3: Verify zero brain imports**

Run: `grep -r "from brain" soul_mesh/`
Expected: No matches

---

## Batch Summary

| Batch | Tasks | What |
|-------|-------|------|
| 1 | Tasks 1-2 | MeshDB extensions + sync.py with tests |
| 2 | Task 3 | Integration verification |
