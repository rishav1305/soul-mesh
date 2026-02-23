"""FastAPI server for the soul-mesh hub.

Exposes REST endpoints for health checks, cluster status, node listing,
and node identity, plus a WebSocket endpoint for agent heartbeats.
FastAPI is an optional dependency (``pip install soul-mesh[server]``),
so we import it inside ``create_app`` to avoid hard failures when only
the core library is used.

Note: ``from __future__ import annotations`` is intentionally omitted.
PEP 563 deferred evaluation turns type hints into strings, which breaks
FastAPI's ``WebSocket`` parameter injection (it sees the string
``"WebSocket"`` instead of the class).  Python 3.10+ supports ``X | Y``
union syntax natively, so the future import is not needed here.
"""

import asyncio
import json

import structlog

from soul_mesh.auth import verify_mesh_token
from soul_mesh.db import MeshDB
from soul_mesh.hub import Hub
from soul_mesh.node import NodeInfo

logger = structlog.get_logger("soul-mesh.server")


async def _stale_sweep_loop(hub, interval: int) -> None:
    """Periodically mark nodes with no recent heartbeat as stale."""
    while True:
        try:
            await asyncio.sleep(interval)
            await hub.mark_stale_nodes(timeout_seconds=interval)
        except asyncio.CancelledError:
            return
        except Exception as exc:
            logger.warning("stale_sweep_error", error=str(exc))


def create_app(db: MeshDB, node: NodeInfo | None = None, *, secret: str = "", stale_interval: int = 30):
    """Create and return a FastAPI application wired to the given database.

    Parameters
    ----------
    db : MeshDB
        The mesh database (tables must already exist via ``ensure_tables``).
    node : NodeInfo | None
        Optional local node identity.  When provided, ``/api/mesh/identity``
        returns this node's info.
    secret : str
        HMAC secret for verifying JWT mesh tokens on the WebSocket endpoint.
        Defaults to empty string (WebSocket auth will reject all tokens).
    """
    from contextlib import asynccontextmanager

    from fastapi import FastAPI, WebSocket, WebSocketDisconnect

    @asynccontextmanager
    async def lifespan(app):
        task = asyncio.create_task(_stale_sweep_loop(app.state.hub, stale_interval))
        yield
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    app = FastAPI(title="soul-mesh", version="0.2.0", lifespan=lifespan)

    # Store shared state on the app so route handlers can access it.
    app.state.db = db
    app.state.hub = Hub(db)
    app.state.node = node
    app.state.secret = secret

    @app.get("/api/mesh/health")
    async def health():
        return {"status": "ok"}

    @app.get("/api/mesh/status")
    async def status():
        return await app.state.hub.cluster_totals()

    @app.get("/api/mesh/nodes")
    async def nodes():
        return await app.state.hub.list_nodes()

    @app.get("/api/mesh/nodes/{node_id}/heartbeats")
    async def node_heartbeats(node_id: str, limit: int = 30):
        return await app.state.hub.heartbeat_history(node_id, limit=max(1, min(limit, 100)))

    @app.get("/api/mesh/identity")
    async def identity():
        n = app.state.node
        if n is None:
            return {"error": "no node identity configured"}
        return {
            "node_id": n.id,
            "name": n.name,
            "port": n.port,
            "platform": n.platform,
            "arch": n.arch,
        }

    @app.websocket("/api/mesh/ws")
    async def websocket_heartbeat(websocket: WebSocket):
        token = websocket.query_params.get("token")

        # Reject missing token
        if not token:
            await websocket.accept()
            await websocket.close(code=4001, reason="missing token")
            return

        # Validate JWT
        try:
            claims = verify_mesh_token(token, app.state.secret)
        except Exception:
            await websocket.accept()
            await websocket.close(code=4003, reason="invalid token")
            return

        await websocket.accept()
        node_id = claims["node_id"]
        first_message = True

        logger.info("ws_connected", node_id=node_id)

        try:
            while True:
                try:
                    data = await websocket.receive_json()
                except json.JSONDecodeError:
                    await websocket.send_json({"error": "invalid JSON"})
                    continue

                if first_message:
                    if data.get("node_id") != node_id:
                        await websocket.close(code=4002, reason="node_id mismatch")
                        return
                    await app.state.hub.register_node(data)
                    first_message = False
                else:
                    await app.state.hub.process_heartbeat(node_id, data)

                await websocket.send_json({"status": "ok"})
        except WebSocketDisconnect:
            logger.info("ws_disconnected", node_id=node_id)

    return app
