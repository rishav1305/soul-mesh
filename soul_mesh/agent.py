"""Agent heartbeat loop -- connects to hub and reports resource snapshots.

An agent node periodically sends heartbeat messages containing its identity
and live system metrics (CPU, memory, storage) to the hub over a WebSocket
connection.  On connection failure the loop uses exponential backoff before
retrying.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import structlog

from soul_mesh.auth import create_mesh_token
from soul_mesh.config import MeshConfig
from soul_mesh.node import NodeInfo
from soul_mesh.resources import get_system_snapshot

logger = structlog.get_logger("soul-mesh.agent")

# Backoff constants
_BACKOFF_BASE: float = 1.0
_BACKOFF_MAX: float = 60.0
_BACKOFF_FACTOR: float = 2.0


class Agent:
    """Agent node that heartbeats to a hub.

    Parameters
    ----------
    config : MeshConfig
        Mesh configuration (must have ``hub`` set to the hub address).
    node_id_path : str | Path | None
        Path to persist the node UUID.  Pass ``":memory:"`` for tests
        to avoid filesystem side-effects.
    """

    def __init__(
        self,
        config: MeshConfig,
        node_id_path: str | Path | None = None,
    ) -> None:
        self.config = config
        self.running: bool = False
        self._ws = None
        self._task: asyncio.Task | None = None
        self._node = NodeInfo(
            node_name=config.name,
            port=config.port,
            node_id_path=node_id_path or ":memory:",
        )

    async def _init_node(self) -> None:
        """Initialize node identity (load or create UUID, gather system info)."""
        await self._node.init()
        logger.info(
            "node_initialized",
            node_id=self._node.id[:8],
            name=self._node.name,
            platform=self._node.platform,
        )

    async def _build_heartbeat(self) -> dict:
        """Build a heartbeat payload merging node identity with live resources.

        Returns
        -------
        dict
            Keys: ``node_id``, ``name``, ``host``, ``port``, ``platform``,
            ``arch``, ``cpu``, ``memory``, ``storage``.
        """
        snapshot = await get_system_snapshot()
        return {
            "node_id": self._node.id,
            "name": self._node.name,
            "host": self._node.host,
            "port": self._node.port,
            "platform": self._node.platform,
            "arch": self._node.arch,
            **snapshot,
        }

    async def start(self) -> None:
        """Start the agent: initialize node and launch heartbeat loop."""
        self.running = True
        await self._init_node()
        self._task = asyncio.create_task(self._heartbeat_loop())
        logger.info("agent_started", hub=self.config.hub)

    async def stop(self) -> None:
        """Stop the agent: cancel heartbeat loop and close WebSocket."""
        self.running = False

        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None

        logger.info("agent_stopped")

    async def _ensure_connection(self):
        """Lazily connect to the hub WebSocket, creating a JWT token."""
        if self._ws is not None:
            return

        import websockets

        token = create_mesh_token(
            node_id=self._node.id,
            account_id=self._node.account_id,
            secret=self.config.secret,
        )
        url = f"ws://{self.config.hub}/api/mesh/ws?token={token}"
        logger.debug("connecting_to_hub", url=url[:80])
        self._ws = await websockets.connect(url)
        logger.info("connected_to_hub", hub=self.config.hub)

    async def _heartbeat_loop(self) -> None:
        """Send heartbeats to the hub at the configured interval.

        Uses exponential backoff on connection or send failures.
        """
        backoff = _BACKOFF_BASE

        while self.running:
            try:
                await self._ensure_connection()
                heartbeat = await self._build_heartbeat()
                await self._ws.send(json.dumps(heartbeat))
                logger.debug("heartbeat_sent", node_id=self._node.id[:8])
                backoff = _BACKOFF_BASE  # reset on success
                await asyncio.sleep(self.config.heartbeat_interval)

            except asyncio.CancelledError:
                raise

            except Exception as exc:
                logger.warning(
                    "heartbeat_failed",
                    error=str(exc),
                    backoff=backoff,
                )
                # Close broken connection so _ensure_connection retries
                if self._ws is not None:
                    try:
                        await self._ws.close()
                    except Exception:
                        pass
                    self._ws = None

                await asyncio.sleep(backoff)
                backoff = min(backoff * _BACKOFF_FACTOR, _BACKOFF_MAX)
