"""Command relay -- routes commands from REST API to agents via WebSocket.

The hub maintains a mapping of connected WebSocket objects keyed by node_id.
When a command request arrives via the ``POST /api/mesh/run`` endpoint, the
relay sends the command over the agent's WebSocket and awaits the response.

This module is intentionally decoupled from FastAPI so it can be tested
without importing the server.
"""

from __future__ import annotations

import asyncio
import uuid

import structlog

logger = structlog.get_logger("soul-mesh.executor")


class CommandRelay:
    """Routes commands from REST API to agents via their WebSocket connections.

    The relay tracks three things:

    1. **connections** -- ``node_id -> websocket`` for every connected agent.
    2. **pending commands** -- ``cmd_id -> Future`` for commands awaiting results.

    When ``send_command`` is called, the relay:
    - looks up the agent's WebSocket by *node_id*,
    - creates a ``Future`` keyed by a fresh *cmd_id*,
    - sends a ``run_command`` message to the agent,
    - awaits the future (with timeout).

    The agent is expected to execute the command locally and send back a
    ``command_result`` message containing the *cmd_id*.  The server's WS
    handler calls ``deliver_result`` which resolves the matching future.
    """

    def __init__(self) -> None:
        self._connections: dict[str, object] = {}  # node_id -> websocket
        self._pending: dict[str, asyncio.Future] = {}  # cmd_id -> future

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    def register(self, node_id: str, ws: object) -> None:
        """Store the WebSocket reference for *node_id*."""
        self._connections[node_id] = ws
        logger.debug("relay_registered", node_id=node_id[:8])

    def unregister(self, node_id: str) -> None:
        """Remove the WebSocket reference for *node_id*."""
        self._connections.pop(node_id, None)
        logger.debug("relay_unregistered", node_id=node_id[:8])

    def get(self, node_id: str) -> object | None:
        """Look up the WebSocket for *node_id*, or ``None``."""
        return self._connections.get(node_id)

    # ------------------------------------------------------------------
    # Command execution
    # ------------------------------------------------------------------

    async def send_command(
        self,
        node_id: str,
        command: str,
        timeout: float = 30.0,
    ) -> dict:
        """Send *command* to the agent identified by *node_id*.

        Returns the result dict with keys ``cmd_id``, ``stdout``, ``stderr``,
        ``exit_code``.

        Raises
        ------
        ValueError
            If *node_id* is not connected.
        asyncio.TimeoutError
            If the agent does not respond within *timeout* seconds.
        """
        ws = self._connections.get(node_id)
        if ws is None:
            raise ValueError(f"Node {node_id!r} not connected")

        cmd_id = str(uuid.uuid4())
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        self._pending[cmd_id] = future

        try:
            await ws.send_json({
                "type": "run_command",
                "cmd_id": cmd_id,
                "command": command,
            })
            logger.info(
                "command_sent",
                node_id=node_id[:8],
                cmd_id=cmd_id[:8],
                command=command[:80],
            )
            result = await asyncio.wait_for(future, timeout=timeout)
            return result
        finally:
            self._pending.pop(cmd_id, None)

    def deliver_result(self, cmd_id: str, result: dict) -> None:
        """Resolve the pending future for *cmd_id* with *result*.

        Called by the server's WebSocket handler when the agent sends a
        ``command_result`` message.  Silently ignores unknown *cmd_id* values
        (e.g. if the request already timed out).
        """
        future = self._pending.get(cmd_id)
        if future is not None and not future.done():
            future.set_result(result)
            logger.debug("result_delivered", cmd_id=cmd_id[:8])
        else:
            logger.warning("result_orphaned", cmd_id=cmd_id[:8])
