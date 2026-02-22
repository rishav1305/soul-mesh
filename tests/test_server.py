"""Tests for the FastAPI server -- health, status, nodes, identity endpoints."""

from __future__ import annotations

import pytest
import httpx

from soul_mesh.db import MeshDB
from soul_mesh.hub import Hub
from soul_mesh.node import NodeInfo
from soul_mesh.server import create_app


@pytest.fixture
async def db():
    db = MeshDB(":memory:")
    await db.ensure_tables()
    return db


@pytest.fixture
def node():
    """A lightweight NodeInfo without calling init() (no subprocess)."""
    n = NodeInfo(node_name="test-node", port=8340, node_id_path=":memory:")
    n.id = "test-node-id-1234"
    n.platform = "linux"
    n.arch = "aarch64"
    return n


@pytest.fixture
async def client(db, node):
    app = create_app(db, node=node)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _make_registration(
    node_id: str = "node-1",
    name: str = "pi-node",
    host: str = "192.168.1.10",
    port: int = 8340,
    platform: str = "linux",
    arch: str = "aarch64",
    cpu_cores: int = 4,
    ram_total: int = 8192,
) -> dict:
    """Build a node registration payload for Hub.register_node."""
    return {
        "node_id": node_id,
        "name": name,
        "host": host,
        "port": port,
        "platform": platform,
        "arch": arch,
        "cpu": {"cores": cpu_cores, "usage_percent": 25.0, "load_avg_1m": 1.0},
        "memory": {"total_mb": ram_total, "available_mb": ram_total // 2, "used_percent": 50.0},
        "storage": {"mounts": [{"path": "/", "total_gb": 256, "free_gb": 100}]},
    }


class TestHealth:
    """GET /api/mesh/health -- basic liveness check."""

    async def test_health_returns_200(self, client):
        resp = await client.get("/api/mesh/health")
        assert resp.status_code == 200

    async def test_health_body(self, client):
        resp = await client.get("/api/mesh/health")
        assert resp.json() == {"status": "ok"}


class TestStatusEmpty:
    """GET /api/mesh/status -- empty cluster."""

    async def test_status_empty_cluster(self, client):
        resp = await client.get("/api/mesh/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["nodes_online"] == 0
        assert data["cpu_cores"] == 0
        assert data["ram_total_mb"] == 0


class TestStatusWithNodes:
    """GET /api/mesh/status -- cluster with registered nodes."""

    async def test_status_aggregates_resources(self, client, db):
        hub = Hub(db)
        await hub.register_node(_make_registration(node_id="s-1", cpu_cores=4, ram_total=8192))
        await hub.register_node(_make_registration(node_id="s-2", cpu_cores=8, ram_total=16384))
        resp = await client.get("/api/mesh/status")
        data = resp.json()
        assert data["nodes_online"] == 2
        assert data["cpu_cores"] == 12
        assert data["ram_total_mb"] == 24576


class TestNodesEmpty:
    """GET /api/mesh/nodes -- empty registry."""

    async def test_nodes_empty(self, client):
        resp = await client.get("/api/mesh/nodes")
        assert resp.status_code == 200
        assert resp.json() == []


class TestNodesWithRegistered:
    """GET /api/mesh/nodes -- with registered nodes."""

    async def test_nodes_returns_list(self, client, db):
        hub = Hub(db)
        await hub.register_node(_make_registration(node_id="n-1", name="alpha", cpu_cores=4, ram_total=8192))
        resp = await client.get("/api/mesh/nodes")
        data = resp.json()
        assert len(data) == 1
        assert data[0]["name"] == "alpha"
        assert data[0]["cpu_cores"] == 4
        assert data[0]["ram_total_mb"] == 8192


class TestIdentity:
    """GET /api/mesh/identity -- returns this node's identity."""

    async def test_identity_returns_node_info(self, client):
        resp = await client.get("/api/mesh/identity")
        assert resp.status_code == 200
        data = resp.json()
        assert data["node_id"] == "test-node-id-1234"
        assert data["name"] == "test-node"
        assert data["port"] == 8340
        assert data["platform"] == "linux"
        assert data["arch"] == "aarch64"
