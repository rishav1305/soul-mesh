"""FastAPI server for the soul-mesh hub.

Exposes REST endpoints for health checks, cluster status, node listing,
and node identity.  FastAPI is an optional dependency (``pip install
soul-mesh[server]``), so we import it inside ``create_app`` to avoid
hard failures when only the core library is used.
"""

from __future__ import annotations

from soul_mesh.db import MeshDB
from soul_mesh.hub import Hub
from soul_mesh.node import NodeInfo


def create_app(db: MeshDB, node: NodeInfo | None = None):
    """Create and return a FastAPI application wired to the given database.

    Parameters
    ----------
    db : MeshDB
        The mesh database (tables must already exist via ``ensure_tables``).
    node : NodeInfo | None
        Optional local node identity.  When provided, ``/api/mesh/identity``
        returns this node's info.
    """
    from fastapi import FastAPI

    app = FastAPI(title="soul-mesh", version="0.2.0")

    # Store shared state on the app so route handlers can access it.
    app.state.db = db
    app.state.hub = Hub(db)
    app.state.node = node

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

    return app
