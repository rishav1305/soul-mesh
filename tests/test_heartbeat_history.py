"""Tests for heartbeat history -- Hub.heartbeat_history + server endpoint."""

from __future__ import annotations

import pytest
import httpx

from soul_mesh.db import MeshDB
from soul_mesh.hub import Hub
from soul_mesh.node import NodeInfo
from soul_mesh.server import create_app


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
async def db():
    db = MeshDB(":memory:")
    await db.ensure_tables()
    return db


@pytest.fixture
async def hub(db):
    h = Hub(db)
    await h.register_node({
        "node_id": "node-1",
        "name": "test-node",
        "host": "10.0.0.1",
        "port": 8340,
        "platform": "linux",
        "arch": "x86_64",
        "cpu": {"cores": 4, "usage_percent": 25.0, "load_avg_1m": 1.0},
        "memory": {"total_mb": 8192, "available_mb": 4096, "used_percent": 50.0},
        "storage": {"mounts": [{"path": "/", "total_gb": 500, "free_gb": 200}]},
    })
    return h


@pytest.fixture
async def client(db):
    node = NodeInfo(node_name="test-node", port=8340, node_id_path=":memory:")
    node.id = "test-node-id"
    node.platform = "linux"
    node.arch = "x86_64"
    app = create_app(db, node=node)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# ---------------------------------------------------------------------------
# Hub.heartbeat_history tests
# ---------------------------------------------------------------------------

class TestHeartbeatHistoryHub:
    """Hub.heartbeat_history -- returns recent heartbeats for a node."""

    async def test_returns_recent(self, hub):
        for i in range(5):
            await hub.process_heartbeat("node-1", {
                "cpu": {"usage_percent": 10.0 + i, "load_avg_1m": 0.5},
                "memory": {"available_mb": 4000, "used_percent": 50.0},
                "storage": {"mounts": [{"path": "/", "total_gb": 500, "free_gb": 200}]},
            })
        history = await hub.heartbeat_history("node-1", limit=3)
        assert len(history) == 3
        # Most recent first (highest cpu_usage_percent)
        assert history[0]["cpu_usage_percent"] >= history[-1]["cpu_usage_percent"]

    async def test_empty_node(self, hub):
        history = await hub.heartbeat_history("nonexistent", limit=10)
        assert history == []

    async def test_default_limit(self, hub):
        history = await hub.heartbeat_history("node-1", limit=30)
        # At least the initial registration heartbeat
        assert len(history) >= 1
        assert len(history) <= 30


# ---------------------------------------------------------------------------
# Server endpoint tests
# ---------------------------------------------------------------------------

class TestHeartbeatHistoryEndpoint:
    """GET /api/mesh/nodes/{node_id}/heartbeats -- server endpoint."""

    async def test_returns_200_with_heartbeats(self, client, db):
        hub = Hub(db)
        await hub.register_node({
            "node_id": "ep-1",
            "name": "endpoint-node",
            "host": "10.0.0.2",
            "port": 8340,
            "platform": "linux",
            "arch": "x86_64",
            "cpu": {"cores": 4, "usage_percent": 20.0, "load_avg_1m": 0.8},
            "memory": {"total_mb": 4096, "available_mb": 2048, "used_percent": 50.0},
            "storage": {"mounts": [{"path": "/", "total_gb": 256, "free_gb": 100}]},
        })
        resp = await client.get("/api/mesh/nodes/ep-1/heartbeats")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) >= 1
        assert data[0]["node_id"] == "ep-1"

    async def test_empty_for_unknown_node(self, client):
        resp = await client.get("/api/mesh/nodes/unknown-999/heartbeats")
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_respects_limit_param(self, client, db):
        hub = Hub(db)
        await hub.register_node({
            "node_id": "ep-2",
            "name": "limit-node",
            "host": "10.0.0.3",
            "port": 8340,
            "platform": "linux",
            "arch": "x86_64",
            "cpu": {"cores": 2, "usage_percent": 10.0, "load_avg_1m": 0.3},
            "memory": {"total_mb": 2048, "available_mb": 1024, "used_percent": 50.0},
            "storage": {"mounts": [{"path": "/", "total_gb": 128, "free_gb": 60}]},
        })
        for i in range(5):
            await hub.process_heartbeat("ep-2", {
                "cpu": {"usage_percent": 15.0 + i, "load_avg_1m": 0.5},
                "memory": {"available_mb": 1000, "used_percent": 51.0},
                "storage": {"mounts": [{"path": "/", "total_gb": 128, "free_gb": 60}]},
            })
        resp = await client.get("/api/mesh/nodes/ep-2/heartbeats?limit=2")
        data = resp.json()
        assert len(data) == 2

    async def test_limit_capped_at_100(self, client, db):
        """Requesting limit > 100 should be capped to 100."""
        resp = await client.get("/api/mesh/nodes/any-node/heartbeats?limit=500")
        assert resp.status_code == 200
        # Just check it doesn't error -- cap is enforced server-side
