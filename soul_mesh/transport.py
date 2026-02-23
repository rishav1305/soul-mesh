"""Mesh transport -- authenticated WebSocket communication.

Extracted from soul-os. Uses standalone MeshDB and auth modules
instead of brain.db.store and brain.auth.jwt.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Awaitable, Callable

import structlog

from soul_mesh.auth import create_mesh_token
from soul_mesh.db import MeshDB

logger = structlog.get_logger("soul-mesh.transport")

MAX_RECONNECT_DELAY = 300
MAX_MESSAGE_SIZE = 1_048_576  # 1 MiB
HEARTBEAT_INTERVAL = 30


class MeshTransport:
    """WebSocket-based mesh transport with auth and reconnection.

    Parameters
    ----------
    local_node : NodeInfo
        The local node instance.
    db : MeshDB
        Database wrapper for peer lookups.
    secret : str
        JWT signing secret for mesh tokens.
    """

    def __init__(self, local_node, db: MeshDB, secret: str) -> None:
        self._local = local_node
        self._db = db
        self._secret = secret
        self._handlers: dict[str, Callable[..., Awaitable]] = {}
        self._connections: dict[str, Any] = {}
        self._outbound_tasks: dict[str, asyncio.Task] = {}
        self._running = False

    def on(self, msg_type: str, handler: Callable[..., Awaitable]) -> None:
        """Register a handler for a mesh message type."""
        self._handlers[msg_type] = handler

    async def start(self) -> None:
        self._running = True
        peers = await self._db.fetch_all(
            "SELECT id, host, port FROM nodes "
            "WHERE id != ? AND status = 'online'",
            (self._local.id,),
        )
        for peer in peers:
            self._start_outbound(peer["id"], peer["host"], peer["port"])

    async def stop(self) -> None:
        self._running = False
        tasks = list(self._outbound_tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for pid, ws in list(self._connections.items()):
            try:
                await ws.close()
            except Exception as exc:
                logger.debug("Error closing WS", peer_id=pid[:8], error=str(exc))
        self._connections.clear()
        self._outbound_tasks.clear()
        logger.info("Transport stopped")

    # -- outbound ---------------------------------------------------

    def _start_outbound(self, peer_id: str, host: str, port: int) -> None:
        if peer_id in self._outbound_tasks:
            return
        self._outbound_tasks[peer_id] = asyncio.create_task(
            self._connect_with_backoff(peer_id, host, port)
        )

    async def _connect_with_backoff(
        self, peer_id: str, host: str, port: int
    ) -> None:
        delay = 1
        while self._running:
            try:
                await self._connect_to_peer(peer_id, host, port)
                delay = 1
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.warning(
                    "Connection failed",
                    peer_id=peer_id[:8],
                    error=str(exc),
                    retry_delay=delay,
                )
            await asyncio.sleep(delay)
            delay = min(delay * 2, MAX_RECONNECT_DELAY)

    async def _connect_to_peer(
        self, peer_id: str, host: str, port: int
    ) -> None:
        token = create_mesh_token(
            self._local.id, self._local.account_id, self._secret
        )
        uri = f"ws://{host}:{port}/api/mesh/ws?token={token}"

        import websockets

        async with websockets.connect(
            uri,
            max_size=MAX_MESSAGE_SIZE,
            ping_interval=HEARTBEAT_INTERVAL,
            ping_timeout=HEARTBEAT_INTERVAL,
            close_timeout=5,
        ) as ws:
            self._connections[peer_id] = ws
            logger.info("Connected to peer", peer_id=peer_id[:8], host=host, port=port)
            try:
                async for raw in ws:
                    if len(raw) > MAX_MESSAGE_SIZE:
                        logger.warning(
                            "Oversized message dropped",
                            peer_id=peer_id[:8],
                            size=len(raw),
                        )
                        continue
                    try:
                        data = json.loads(raw)
                    except json.JSONDecodeError:
                        logger.warning("Malformed JSON dropped", peer_id=peer_id[:8])
                        continue
                    await self.handle_message(data, peer_id)
            finally:
                self._connections.pop(peer_id, None)

    # -- inbound ----------------------------------------------------

    async def register_inbound(self, peer_id: str, websocket: Any) -> None:
        self._connections[peer_id] = websocket
        logger.info("Peer connected (inbound)", peer_id=peer_id[:8])

    async def unregister(self, peer_id: str) -> None:
        self._connections.pop(peer_id, None)
        task = self._outbound_tasks.pop(peer_id, None)
        if task:
            task.cancel()
        logger.info("Peer disconnected", peer_id=peer_id[:8])

    # -- sending ----------------------------------------------------

    async def send(self, peer_id: str, msg_type: str, payload: Any) -> None:
        ws = self._connections.get(peer_id)
        if not ws:
            raise ConnectionError(f"No connection to peer {peer_id[:8]}")
        msg = json.dumps({
            "type": msg_type,
            "payload": payload,
            "from": self._local.id,
        })
        if len(msg) > MAX_MESSAGE_SIZE:
            raise ValueError(
                f"Message too large: {len(msg)} bytes (max {MAX_MESSAGE_SIZE})"
            )
        if hasattr(ws, "send_text"):
            await ws.send_text(msg)
        else:
            await ws.send(msg)

    async def send_to_hub(self, msg_type: str, payload: Any) -> None:
        hub = await self._db.fetch_one(
            "SELECT id FROM nodes WHERE role = 'hub' AND status = 'online'"
        )
        if not hub:
            raise ConnectionError("No hub available")
        await self.send(hub["id"], msg_type, payload)

    async def broadcast(self, msg_type: str, payload: Any) -> None:
        for peer_id in list(self._connections):
            try:
                await self.send(peer_id, msg_type, payload)
            except Exception as exc:
                logger.warning("Broadcast failed", peer_id=peer_id[:8], error=str(exc))

    # -- routing ----------------------------------------------------

    async def handle_message(self, data: dict, peer_id: str) -> Any:
        msg_type = data.get("type", "")
        handler = self._handlers.get(msg_type)
        if not handler:
            logger.warning("No handler for message type", msg_type=msg_type)
            return None
        try:
            return await handler(data.get("payload"), peer_id)
        except Exception as exc:
            logger.error(
                "Handler failed", msg_type=msg_type, error=str(exc), exc_info=True
            )
            return None
