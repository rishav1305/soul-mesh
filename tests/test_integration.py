"""Integration test -- agent heartbeat -> hub registration."""

from __future__ import annotations

import pytest

from soul_mesh.auth import create_mesh_token
from soul_mesh.db import MeshDB
from soul_mesh.hub import Hub
from soul_mesh.server import create_app


class TestAgentHubIntegration:
    """End-to-end: agent sends heartbeat over WebSocket, hub registers it."""

    @pytest.fixture
    async def db(self, tmp_path):
        db = MeshDB(str(tmp_path / "test.db"))
        await db.ensure_tables()
        return db

    async def test_heartbeat_registers_node(self, db):
        """Send a heartbeat via WebSocket and verify node appears in DB."""
        secret = "integration-test-secret-32-bytes!"
        app = create_app(db, secret=secret)

        from starlette.testclient import TestClient

        token = create_mesh_token("agent-1", "acct-1", secret)
        client = TestClient(app)

        with client.websocket_connect(f"/api/mesh/ws?token={token}") as ws:
            ws.send_json({
                "node_id": "agent-1",
                "name": "my-pi",
                "host": "192.168.1.50",
                "port": 8340,
                "platform": "linux",
                "arch": "aarch64",
                "cpu": {"cores": 4, "usage_percent": 12.0, "load_avg_1m": 0.3},
                "memory": {"total_mb": 4096, "available_mb": 2048, "used_percent": 50.0},
                "storage": {"mounts": [{"path": "/", "total_gb": 64, "free_gb": 40}]},
            })
            resp = ws.receive_json()
            assert resp["status"] == "ok"

        # Verify in DB
        nodes = await db.fetch_all("SELECT * FROM nodes")
        assert len(nodes) == 1
        assert nodes[0]["id"] == "agent-1"
        assert nodes[0]["name"] == "my-pi"
        assert nodes[0]["cpu_cores"] == 4
        assert nodes[0]["ram_total_mb"] == 4096
        assert nodes[0]["status"] == "online"

        # Verify heartbeat recorded
        heartbeats = await db.fetch_all("SELECT * FROM heartbeats WHERE node_id = 'agent-1'")
        assert len(heartbeats) == 1

    async def test_cluster_totals_after_heartbeats(self, db):
        """Two agents heartbeat, cluster totals reflect both."""
        secret = "integration-test-secret-32-bytes!"
        app = create_app(db, secret=secret)

        from starlette.testclient import TestClient
        client = TestClient(app)

        for i, (cores, ram) in enumerate([(4, 4096), (8, 16384)], start=1):
            token = create_mesh_token(f"agent-{i}", "acct-1", secret)
            with client.websocket_connect(f"/api/mesh/ws?token={token}") as ws:
                ws.send_json({
                    "node_id": f"agent-{i}",
                    "name": f"node-{i}",
                    "host": f"10.0.0.{i}",
                    "port": 8340,
                    "platform": "linux",
                    "arch": "x86_64",
                    "cpu": {"cores": cores, "usage_percent": 10.0, "load_avg_1m": 0.1},
                    "memory": {"total_mb": ram, "available_mb": ram // 2, "used_percent": 50.0},
                    "storage": {"mounts": [{"path": "/", "total_gb": 500, "free_gb": 250}]},
                })
                ws.receive_json()

        hub = Hub(db)
        totals = await hub.cluster_totals()
        assert totals["nodes_online"] == 2
        assert totals["cpu_cores"] == 12
        assert totals["ram_total_mb"] == 20480
