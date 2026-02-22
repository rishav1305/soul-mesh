"""Tests for the hub control plane -- node registry, heartbeats, resource aggregation."""

from __future__ import annotations

from datetime import datetime, timezone, timedelta

import pytest

from soul_mesh.db import MeshDB
from soul_mesh.hub import Hub


@pytest.fixture
async def db():
    db = MeshDB(":memory:")
    await db.ensure_tables()
    return db


@pytest.fixture
async def hub(db):
    return Hub(db)


def _make_registration(
    node_id: str = "node-1",
    name: str = "pi-node",
    host: str = "192.168.1.10",
    port: int = 8340,
    platform: str = "linux",
    arch: str = "aarch64",
    cpu_cores: int = 4,
    cpu_usage: float = 25.0,
    cpu_load: float = 1.2,
    ram_total: int = 8192,
    ram_available: int = 4096,
    ram_used: float = 50.0,
    storage_mounts: list[dict] | None = None,
) -> dict:
    """Build a node registration payload matching the Hub.register_node contract."""
    if storage_mounts is None:
        storage_mounts = [{"path": "/", "total_gb": 256, "free_gb": 100}]
    return {
        "node_id": node_id,
        "name": name,
        "host": host,
        "port": port,
        "platform": platform,
        "arch": arch,
        "cpu": {"cores": cpu_cores, "usage_percent": cpu_usage, "load_avg_1m": cpu_load},
        "memory": {"total_mb": ram_total, "available_mb": ram_available, "used_percent": ram_used},
        "storage": {"mounts": storage_mounts},
    }


def _make_snapshot(
    cpu_usage: float = 30.0,
    cpu_load: float = 1.5,
    ram_available: int = 3000,
    ram_used: float = 63.0,
    storage_mounts: list[dict] | None = None,
) -> dict:
    """Build a heartbeat snapshot payload."""
    if storage_mounts is None:
        storage_mounts = [{"path": "/", "total_gb": 256, "free_gb": 80}]
    return {
        "cpu": {"usage_percent": cpu_usage, "load_avg_1m": cpu_load},
        "memory": {"available_mb": ram_available, "used_percent": ram_used},
        "storage": {"mounts": storage_mounts},
    }


class TestRegisterNode:
    """Hub.register_node -- inserts node, sets status, records heartbeat."""

    async def test_registers_node_in_db(self, hub, db):
        data = _make_registration(node_id="reg-1", name="alpha")
        await hub.register_node(data)
        row = await db.fetch_one("SELECT * FROM nodes WHERE id = ?", ("reg-1",))
        assert row is not None
        assert row["name"] == "alpha"

    async def test_sets_status_online(self, hub, db):
        await hub.register_node(_make_registration(node_id="reg-2"))
        row = await db.fetch_one("SELECT status FROM nodes WHERE id = ?", ("reg-2",))
        assert row["status"] == "online"

    async def test_records_cpu_cores(self, hub, db):
        await hub.register_node(_make_registration(node_id="reg-3", cpu_cores=8))
        row = await db.fetch_one("SELECT cpu_cores FROM nodes WHERE id = ?", ("reg-3",))
        assert row["cpu_cores"] == 8

    async def test_records_initial_heartbeat(self, hub, db):
        await hub.register_node(_make_registration(node_id="reg-4"))
        rows = await db.fetch_all(
            "SELECT * FROM heartbeats WHERE node_id = ?", ("reg-4",)
        )
        assert len(rows) == 1
        assert rows[0]["cpu_usage_percent"] == pytest.approx(25.0)

    async def test_records_ram_total_mb(self, hub, db):
        await hub.register_node(_make_registration(node_id="reg-5", ram_total=16384))
        row = await db.fetch_one("SELECT ram_total_mb FROM nodes WHERE id = ?", ("reg-5",))
        assert row["ram_total_mb"] == 16384

    async def test_records_storage_total_gb(self, hub, db):
        mounts = [
            {"path": "/", "total_gb": 200, "free_gb": 100},
            {"path": "/data", "total_gb": 500, "free_gb": 300},
        ]
        await hub.register_node(_make_registration(node_id="reg-6", storage_mounts=mounts))
        row = await db.fetch_one("SELECT storage_total_gb FROM nodes WHERE id = ?", ("reg-6",))
        assert row["storage_total_gb"] == pytest.approx(700.0)


class TestRegisterNodeUpsert:
    """Hub.register_node twice with same id -- upsert behavior."""

    async def test_upsert_updates_name(self, hub, db):
        await hub.register_node(_make_registration(node_id="up-1", name="original"))
        await hub.register_node(_make_registration(node_id="up-1", name="updated"))
        row = await db.fetch_one("SELECT name FROM nodes WHERE id = ?", ("up-1",))
        assert row["name"] == "updated"

    async def test_upsert_still_one_node(self, hub, db):
        await hub.register_node(_make_registration(node_id="up-2"))
        await hub.register_node(_make_registration(node_id="up-2"))
        rows = await db.fetch_all("SELECT id FROM nodes WHERE id = ?", ("up-2",))
        assert len(rows) == 1


class TestProcessHeartbeat:
    """Hub.process_heartbeat -- updates status, records heartbeat row."""

    async def test_updates_status_to_online(self, hub, db):
        await hub.register_node(_make_registration(node_id="hb-1"))
        # Manually set to stale first
        await db.execute("UPDATE nodes SET status = 'stale' WHERE id = ?", ("hb-1",))
        await hub.process_heartbeat("hb-1", _make_snapshot())
        row = await db.fetch_one("SELECT status FROM nodes WHERE id = ?", ("hb-1",))
        assert row["status"] == "online"

    async def test_inserts_heartbeat_row(self, hub, db):
        await hub.register_node(_make_registration(node_id="hb-2"))
        await hub.process_heartbeat("hb-2", _make_snapshot(cpu_usage=55.5))
        rows = await db.fetch_all(
            "SELECT * FROM heartbeats WHERE node_id = ? ORDER BY id", ("hb-2",)
        )
        # 1 from register + 1 from process_heartbeat
        assert len(rows) == 2
        assert rows[1]["cpu_usage_percent"] == pytest.approx(55.5)

    async def test_records_ram_and_storage_in_heartbeat(self, hub, db):
        await hub.register_node(_make_registration(node_id="hb-3"))
        snap = _make_snapshot(ram_available=2048, storage_mounts=[{"path": "/", "total_gb": 500, "free_gb": 200}])
        await hub.process_heartbeat("hb-3", snap)
        rows = await db.fetch_all(
            "SELECT * FROM heartbeats WHERE node_id = ? ORDER BY id", ("hb-3",)
        )
        latest = rows[-1]
        assert latest["ram_available_mb"] == 2048
        assert latest["storage_free_gb"] == pytest.approx(200.0)


class TestMarkStaleNodes:
    """Hub.mark_stale_nodes -- identifies and marks stale nodes."""

    async def test_returns_stale_ids(self, hub, db):
        await hub.register_node(_make_registration(node_id="stale-1"))
        # Set heartbeat to 60 seconds ago
        old_time = (datetime.now(timezone.utc) - timedelta(seconds=60)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        await db.execute(
            "UPDATE nodes SET last_heartbeat = ? WHERE id = ?", (old_time, "stale-1")
        )
        stale_ids = await hub.mark_stale_nodes(timeout_seconds=30)
        assert "stale-1" in stale_ids

    async def test_sets_status_to_stale(self, hub, db):
        await hub.register_node(_make_registration(node_id="stale-2"))
        old_time = (datetime.now(timezone.utc) - timedelta(seconds=120)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        await db.execute(
            "UPDATE nodes SET last_heartbeat = ? WHERE id = ?", (old_time, "stale-2")
        )
        await hub.mark_stale_nodes(timeout_seconds=30)
        row = await db.fetch_one("SELECT status FROM nodes WHERE id = ?", ("stale-2",))
        assert row["status"] == "stale"

    async def test_fresh_nodes_not_marked(self, hub, db):
        await hub.register_node(_make_registration(node_id="fresh-1"))
        # Just registered -- heartbeat is fresh
        stale_ids = await hub.mark_stale_nodes(timeout_seconds=30)
        assert "fresh-1" not in stale_ids
        row = await db.fetch_one("SELECT status FROM nodes WHERE id = ?", ("fresh-1",))
        assert row["status"] == "online"


class TestClusterTotals:
    """Hub.cluster_totals -- aggregate resources across online nodes."""

    async def test_sums_cpu_and_ram(self, hub, db):
        await hub.register_node(_make_registration(node_id="ct-1", cpu_cores=4, ram_total=8192))
        await hub.register_node(_make_registration(node_id="ct-2", cpu_cores=8, ram_total=16384))
        totals = await hub.cluster_totals()
        assert totals["cpu_cores"] == 12
        assert totals["ram_total_mb"] == 24576
        assert totals["nodes_online"] == 2

    async def test_excludes_stale_nodes(self, hub, db):
        await hub.register_node(_make_registration(node_id="ct-3", cpu_cores=4, ram_total=4096))
        await hub.register_node(_make_registration(node_id="ct-4", cpu_cores=2, ram_total=2048))
        await db.execute("UPDATE nodes SET status = 'stale' WHERE id = ?", ("ct-4",))
        totals = await hub.cluster_totals()
        assert totals["cpu_cores"] == 4
        assert totals["ram_total_mb"] == 4096
        assert totals["nodes_online"] == 1

    async def test_includes_storage_total_gb(self, hub, db):
        mounts_a = [{"path": "/", "total_gb": 256, "free_gb": 100}]
        mounts_b = [{"path": "/", "total_gb": 512, "free_gb": 200}]
        await hub.register_node(_make_registration(node_id="ct-5", storage_mounts=mounts_a))
        await hub.register_node(_make_registration(node_id="ct-6", storage_mounts=mounts_b))
        totals = await hub.cluster_totals()
        assert totals["storage_total_gb"] == pytest.approx(768.0)

    async def test_empty_cluster(self, hub):
        totals = await hub.cluster_totals()
        assert totals["nodes_online"] == 0
        assert totals["cpu_cores"] == 0
        assert totals["ram_total_mb"] == 0
        assert totals["storage_total_gb"] == pytest.approx(0.0)


class TestListNodes:
    """Hub.list_nodes -- returns all registered nodes."""

    async def test_returns_all_nodes(self, hub):
        await hub.register_node(_make_registration(node_id="ln-1", name="bravo"))
        await hub.register_node(_make_registration(node_id="ln-2", name="alpha"))
        nodes = await hub.list_nodes()
        assert len(nodes) == 2

    async def test_ordered_by_name(self, hub):
        await hub.register_node(_make_registration(node_id="ln-3", name="charlie"))
        await hub.register_node(_make_registration(node_id="ln-4", name="alpha"))
        nodes = await hub.list_nodes()
        assert nodes[0]["name"] == "alpha"
        assert nodes[1]["name"] == "charlie"

    async def test_empty_returns_empty_list(self, hub):
        nodes = await hub.list_nodes()
        assert nodes == []
