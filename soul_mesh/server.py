"""FastAPI server for the soul-mesh hub.

Exposes REST endpoints for health checks, cluster status, node listing,
and node identity, plus a WebSocket endpoint for agent heartbeats.
FastAPI is an optional dependency (``pip install soul-mesh[server]``),
so we import it inside ``create_app`` to avoid hard failures when only
the core library is used.
"""

import structlog

from soul_mesh.auth import verify_mesh_token
from soul_mesh.db import MeshDB
from soul_mesh.hub import Hub
from soul_mesh.node import NodeInfo

logger = structlog.get_logger("soul-mesh.server")


def create_app(db: MeshDB, node: NodeInfo | None = None, *, secret: str = ""):
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
    from fastapi import FastAPI, WebSocket, WebSocketDisconnect

    app = FastAPI(title="soul-mesh", version="0.2.0")

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
                data = await websocket.receive_json()

                if first_message:
                    await app.state.hub.register_node(data)
                    first_message = False
                else:
                    await app.state.hub.process_heartbeat(node_id, data)

                await websocket.send_json({"status": "ok"})
        except WebSocketDisconnect:
            logger.info("ws_disconnected", node_id=node_id)

    return app
