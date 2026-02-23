"""Tests for the FastAPI server -- health, status, nodes, identity endpoints."""

from __future__ import annotations

import pytest
import httpx

from soul_mesh.auth import create_mesh_token
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


class TestWebSocketEndpoint:
    """Tests for the /api/mesh/ws WebSocket heartbeat endpoint."""

    @pytest.fixture
    async def db(self, tmp_path):
        db = MeshDB(str(tmp_path / "test.db"))
        await db.ensure_tables()
        return db

    @pytest.fixture
    def app(self, db):
        return create_app(db, secret="test-secret-key-32-bytes-long!!!")

    async def test_ws_rejects_missing_token(self, app):
        from starlette.testclient import TestClient
        from starlette.websockets import WebSocketDisconnect
        client = TestClient(app)
        with client.websocket_connect("/api/mesh/ws") as ws:
            # Server accepts then immediately closes with 4001
            with pytest.raises(WebSocketDisconnect) as exc_info:
                ws.receive_json()
            assert exc_info.value.code == 4001

    async def test_ws_rejects_invalid_token(self, app):
        from starlette.testclient import TestClient
        from starlette.websockets import WebSocketDisconnect
        client = TestClient(app)
        with client.websocket_connect("/api/mesh/ws?token=bad-token") as ws:
            # Server accepts then immediately closes with 4003
            with pytest.raises(WebSocketDisconnect) as exc_info:
                ws.receive_json()
            assert exc_info.value.code == 4003

    async def test_ws_accepts_valid_token_and_heartbeat(self, app, db):
        from starlette.testclient import TestClient

        token = create_mesh_token("node-1", "acct-1", "test-secret-key-32-bytes-long!!!")
        client = TestClient(app)
        with client.websocket_connect(f"/api/mesh/ws?token={token}") as ws:
            heartbeat = {
                "node_id": "node-1",
                "name": "test-pi",
                "host": "10.0.0.5",
                "port": 8340,
                "platform": "linux",
                "arch": "aarch64",
                "cpu": {"cores": 4, "usage_percent": 15.0, "load_avg_1m": 0.5},
                "memory": {"total_mb": 4096, "available_mb": 2048, "used_percent": 50.0},
                "storage": {"mounts": [{"path": "/", "total_gb": 64, "free_gb": 30}]},
            }
            ws.send_json(heartbeat)
            response = ws.receive_json()
            assert response["status"] == "ok"

        # Verify node was registered in DB
        nodes = await db.fetch_all("SELECT * FROM nodes WHERE id = 'node-1'")
        assert len(nodes) == 1
        assert nodes[0]["name"] == "test-pi"
        assert nodes[0]["status"] == "online"
        assert nodes[0]["cpu_cores"] == 4

    async def test_ws_multiple_heartbeats(self, app, db):
        from starlette.testclient import TestClient

        token = create_mesh_token("node-2", "acct-1", "test-secret-key-32-bytes-long!!!")
        client = TestClient(app)
        with client.websocket_connect(f"/api/mesh/ws?token={token}") as ws:
            heartbeat = {
                "node_id": "node-2",
                "name": "pi",
                "host": "10.0.0.6",
                "port": 8340,
                "platform": "linux",
                "arch": "aarch64",
                "cpu": {"cores": 4, "usage_percent": 10.0, "load_avg_1m": 0.3},
                "memory": {"total_mb": 2048, "available_mb": 1024, "used_percent": 50.0},
                "storage": {"mounts": [{"path": "/", "total_gb": 32, "free_gb": 20}]},
            }
            ws.send_json(heartbeat)
            ws.receive_json()
            # Second heartbeat
            heartbeat["cpu"]["usage_percent"] = 50.0
            ws.send_json(heartbeat)
            resp2 = ws.receive_json()
            assert resp2["status"] == "ok"

        # Should have 2 heartbeat rows
        heartbeats = await db.fetch_all("SELECT * FROM heartbeats WHERE node_id = 'node-2'")
        assert len(heartbeats) == 2
