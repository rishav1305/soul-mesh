"""Tests for the MeshDB async SQLite wrapper."""

from __future__ import annotations

import pytest

from soul_mesh.db import MeshDB


class TestMeshDB:
    @pytest.fixture
    async def db(self, tmp_path):
        """Create a MeshDB with a temp database."""
        db_path = str(tmp_path / "test.db")
        db = MeshDB(db_path)
        await db.ensure_tables()
        return db

    async def test_ensure_tables_creates_mesh_nodes(self, db):
        rows = await db.fetch_all(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='mesh_nodes'"
        )
        assert len(rows) == 1
        assert rows[0]["name"] == "mesh_nodes"

    async def test_execute_and_fetch_one(self, db):
        await db.execute(
            "INSERT INTO mesh_nodes (id, name, status) VALUES (?, ?, ?)",
            ("node-1", "test-node", "online"),
        )
        row = await db.fetch_one(
            "SELECT id, name, status FROM mesh_nodes WHERE id = ?", ("node-1",)
        )
        assert row is not None
        assert row["id"] == "node-1"
        assert row["name"] == "test-node"

    async def test_fetch_one_returns_none_when_missing(self, db):
        row = await db.fetch_one(
            "SELECT id FROM mesh_nodes WHERE id = ?", ("nonexistent",)
        )
        assert row is None

    async def test_fetch_all_returns_list_of_dicts(self, db):
        await db.execute(
            "INSERT INTO mesh_nodes (id, name, status) VALUES (?, ?, ?)",
            ("a", "alpha", "online"),
        )
        await db.execute(
            "INSERT INTO mesh_nodes (id, name, status) VALUES (?, ?, ?)",
            ("b", "bravo", "offline"),
        )
        rows = await db.fetch_all("SELECT id, name FROM mesh_nodes ORDER BY id")
        assert len(rows) == 2
        assert rows[0]["id"] == "a"
        assert rows[1]["id"] == "b"

    async def test_fetch_all_empty_returns_empty_list(self, db):
        rows = await db.fetch_all("SELECT id FROM mesh_nodes")
        assert rows == []

    async def test_execute_with_no_params(self, db):
        await db.execute(
            "INSERT INTO mesh_nodes (id, name, status) VALUES ('x', 'x', 'online')"
        )
        row = await db.fetch_one("SELECT id FROM mesh_nodes WHERE id = 'x'")
        assert row is not None

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

    async def test_in_memory_db(self):
        db = MeshDB(":memory:")
        await db.ensure_tables()
        await db.execute(
            "INSERT INTO mesh_nodes (id, name, status) VALUES (?, ?, ?)",
            ("m1", "mem", "online"),
        )
        row = await db.fetch_one("SELECT id FROM mesh_nodes WHERE id = ?", ("m1",))
        assert row is not None
