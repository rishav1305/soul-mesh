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
