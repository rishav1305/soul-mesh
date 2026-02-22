"""Tests for the MeshDB async SQLite wrapper -- cluster schema."""

from __future__ import annotations

import pytest

from soul_mesh.db import MeshDB


class TestConnection:
    """Connection and table creation tests."""

    async def test_memory_db_reuses_connection(self):
        db = MeshDB(":memory:")
        await db.ensure_tables()
        await db.execute(
            "INSERT INTO nodes (id, name) VALUES (?, ?)", ("n1", "alpha")
        )
        # Second call should see the same data (same connection)
        row = await db.fetch_one("SELECT id FROM nodes WHERE id = ?", ("n1",))
        assert row is not None
        assert row["id"] == "n1"

    async def test_ensure_tables_creates_all_tables(self, tmp_path):
        db = MeshDB(str(tmp_path / "test.db"))
        await db.ensure_tables()
        expected = {"nodes", "heartbeats", "settings", "link_codes", "link_attempts"}
        rows = await db.fetch_all(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
        table_names = {r["name"] for r in rows}
        assert expected.issubset(table_names)


class TestCRUD:
    """Basic CRUD operations."""

    @pytest.fixture
    async def db(self):
        db = MeshDB(":memory:")
        await db.ensure_tables()
        return db

    async def test_insert_and_fetch_one(self, db):
        await db.insert("nodes", {"id": "node-1", "name": "test-node"})
        row = await db.fetch_one(
            "SELECT id, name FROM nodes WHERE id = ?", ("node-1",)
        )
        assert row is not None
        assert row["id"] == "node-1"
        assert row["name"] == "test-node"

    async def test_insert_returns_rowid(self, db):
        rowid = await db.insert("nodes", {"id": "node-1", "name": "test"})
        assert rowid is not None
        assert rowid > 0

    async def test_insert_rejects_unknown_table(self, db):
        with pytest.raises(ValueError, match="not in insertable allowlist"):
            await db.insert("fake_table", {"id": "x"})

    async def test_fetch_all(self, db):
        await db.insert("nodes", {"id": "a", "name": "alpha"})
        await db.insert("nodes", {"id": "b", "name": "bravo"})
        rows = await db.fetch_all("SELECT id, name FROM nodes ORDER BY id")
        assert len(rows) == 2
        assert rows[0]["id"] == "a"
        assert rows[1]["id"] == "b"

    async def test_fetch_all_empty_returns_empty_list(self, db):
        rows = await db.fetch_all("SELECT id FROM nodes")
        assert rows == []

    async def test_fetch_one_returns_none(self, db):
        row = await db.fetch_one(
            "SELECT id FROM nodes WHERE id = ?", ("nonexistent",)
        )
        assert row is None

    async def test_execute_update(self, db):
        await db.insert("nodes", {"id": "u1", "name": "before"})
        await db.execute(
            "UPDATE nodes SET name = ? WHERE id = ?", ("after", "u1")
        )
        row = await db.fetch_one("SELECT name FROM nodes WHERE id = ?", ("u1",))
        assert row["name"] == "after"


class TestTransaction:
    """Transaction commit and rollback."""

    @pytest.fixture
    async def db(self):
        db = MeshDB(":memory:")
        await db.ensure_tables()
        return db

    async def test_transaction_commits_on_success(self, db):
        async with db.transaction() as cursor:
            await cursor.execute(
                "INSERT INTO nodes (id, name) VALUES (?, ?)",
                ("txn-1", "committed"),
            )
        row = await db.fetch_one("SELECT id FROM nodes WHERE id = ?", ("txn-1",))
        assert row is not None

    async def test_transaction_rolls_back_on_exception(self, db):
        with pytest.raises(ValueError):
            async with db.transaction() as cursor:
                await cursor.execute(
                    "INSERT INTO nodes (id, name) VALUES (?, ?)",
                    ("txn-2", "will-rollback"),
                )
                raise ValueError("force rollback")
        row = await db.fetch_one("SELECT id FROM nodes WHERE id = ?", ("txn-2",))
        assert row is None


class TestNodeOperations:
    """Node-specific operations: defaults, upsert, heartbeat, online query."""

    @pytest.fixture
    async def db(self):
        db = MeshDB(":memory:")
        await db.ensure_tables()
        return db

    async def test_insert_node_has_correct_defaults(self, db):
        await db.insert("nodes", {"id": "def-1", "name": "default-node"})
        row = await db.fetch_one("SELECT * FROM nodes WHERE id = ?", ("def-1",))
        assert row["role"] == "agent"
        assert row["port"] == 8340
        assert row["status"] == "offline"
        assert row["cpu_cores"] == 0
        assert row["ram_total_mb"] == 0

    async def test_upsert_node_inserts_new(self, db):
        await db.upsert_node({
            "id": "up-1",
            "name": "new-node",
            "host": "192.168.1.10",
            "port": 8341,
            "platform": "linux",
            "arch": "aarch64",
            "cpu_cores": 4,
            "ram_total_mb": 8192,
            "storage_total_gb": 256.0,
            "last_heartbeat": "2026-02-23T00:00:00Z",
        })
        row = await db.fetch_one("SELECT * FROM nodes WHERE id = ?", ("up-1",))
        assert row is not None
        assert row["name"] == "new-node"
        assert row["host"] == "192.168.1.10"
        assert row["port"] == 8341
        assert row["platform"] == "linux"
        assert row["cpu_cores"] == 4
        assert row["ram_total_mb"] == 8192

    async def test_upsert_node_updates_existing(self, db):
        await db.upsert_node({
            "id": "up-2",
            "name": "original",
            "host": "10.0.0.1",
            "port": 8340,
            "platform": "linux",
            "arch": "x86_64",
            "cpu_cores": 2,
            "ram_total_mb": 4096,
            "storage_total_gb": 100.0,
            "last_heartbeat": "2026-02-23T00:00:00Z",
        })
        # Now upsert with updated values
        await db.upsert_node({
            "id": "up-2",
            "name": "updated",
            "host": "10.0.0.2",
            "port": 9999,
            "platform": "darwin",
            "arch": "arm64",
            "cpu_cores": 8,
            "ram_total_mb": 16384,
            "storage_total_gb": 500.0,
            "last_heartbeat": "2026-02-23T01:00:00Z",
        })
        row = await db.fetch_one("SELECT * FROM nodes WHERE id = ?", ("up-2",))
        assert row["name"] == "updated"
        assert row["host"] == "10.0.0.2"
        assert row["port"] == 9999
        assert row["platform"] == "darwin"
        assert row["cpu_cores"] == 8
        assert row["ram_total_mb"] == 16384

    async def test_insert_heartbeat(self, db):
        await db.insert("nodes", {"id": "hb-node", "name": "heartbeat-test"})
        rowid = await db.insert("heartbeats", {
            "node_id": "hb-node",
            "cpu_usage_percent": 45.2,
            "cpu_load_1m": 1.5,
            "ram_available_mb": 2048,
            "ram_used_percent": 75.0,
            "storage_free_gb": 50.0,
        })
        assert rowid > 0
        row = await db.fetch_one(
            "SELECT * FROM heartbeats WHERE id = ?", (rowid,)
        )
        assert row["node_id"] == "hb-node"
        assert row["cpu_usage_percent"] == pytest.approx(45.2)
        assert row["ram_available_mb"] == 2048

    async def test_get_online_nodes(self, db):
        await db.insert("nodes", {"id": "on-1", "name": "online-node", "status": "online"})
        await db.insert("nodes", {"id": "off-1", "name": "offline-node", "status": "offline"})
        await db.insert("nodes", {"id": "on-2", "name": "online-node-2", "status": "online"})
        rows = await db.fetch_all(
            "SELECT id FROM nodes WHERE status = 'online' ORDER BY id"
        )
        assert len(rows) == 2
        assert rows[0]["id"] == "on-1"
        assert rows[1]["id"] == "on-2"


class TestLinkAndSettings:
    """Ensure link_codes, link_attempts, and settings tables work."""

    @pytest.fixture
    async def db(self):
        db = MeshDB(":memory:")
        await db.ensure_tables()
        return db

    async def test_insert_link_code(self, db):
        rowid = await db.insert("link_codes", {
            "code": "ABC123",
            "account_id": "acct-1",
            "expires_at": "2026-12-31T23:59:59Z",
        })
        assert rowid is not None
        row = await db.fetch_one(
            "SELECT code, account_id FROM link_codes WHERE code = ?", ("ABC123",)
        )
        assert row["account_id"] == "acct-1"

    async def test_insert_setting(self, db):
        await db.insert("settings", {"key": "node_name", "value": "pi-node"})
        row = await db.fetch_one(
            "SELECT value FROM settings WHERE key = ?", ("node_name",)
        )
        assert row["value"] == "pi-node"
