"""Mesh sync -- hub-proxy writes with an offline pending queue.

All DB writes from non-hub nodes are forwarded to the hub. When the
hub is unreachable, writes are queued in pending_writes (SQLite-backed).
A periodic replay loop drains the queue once connectivity returns.
"""

from __future__ import annotations

import asyncio
import json

import structlog

from soul_mesh.db import MeshDB

logger = structlog.get_logger("soul-mesh.sync")

MAX_RETRY = 10
REPLAY_BATCH = 50
MAX_PENDING_WRITES = 10_000

_MESH_WRITABLE_TABLES: frozenset[str] = frozenset({
    "events", "tasks", "knowledge", "chat_history",
})

_MESH_WRITABLE_COLUMNS: dict[str, frozenset[str]] = {
    "events": frozenset({"source", "event_type", "payload", "session_id"}),
    "tasks": frozenset({"title", "description", "status", "priority", "metadata"}),
    "knowledge": frozenset({"category", "title", "content", "source", "metadata"}),
    "chat_history": frozenset({"role", "content", "session_id"}),
}


class MeshSync:
    """Hub-proxy write coordinator with offline queue."""

    def __init__(self, local_node, transport, db: MeshDB, sync_interval: int = 30) -> None:
        self._local = local_node
        self._transport = transport
        self._db = db
        self._sync_interval = sync_interval
        self._replay_task: asyncio.Task | None = None

    async def start(self) -> None:
        self._replay_task = asyncio.create_task(self._replay_loop())
        self._transport.on("mesh_write", self._handle_remote_write)

    async def stop(self) -> None:
        if self._replay_task:
            self._replay_task.cancel()
            try:
                await self._replay_task
            except asyncio.CancelledError:
                pass

    async def write(self, table: str, data: dict) -> int | None:
        """Write to DB -- locally if hub, else forward to hub."""
        if self._local.is_hub:
            return await self._db.insert(table, data)

        payload = {"table": table, "data": data}
        try:
            await self._transport.send_to_hub("mesh_write", payload)
            return None
        except Exception as exc:
            count_row = await self._db.fetch_one(
                "SELECT COUNT(*) AS cnt FROM pending_writes "
                "WHERE status = 'pending'"
            )
            depth = count_row["cnt"] if count_row else 0
            if depth >= MAX_PENDING_WRITES:
                await self._db.execute(
                    "DELETE FROM pending_writes WHERE id IN ("
                    "  SELECT id FROM pending_writes WHERE status = 'pending' "
                    "  ORDER BY id ASC LIMIT 100"
                    ")"
                )
                logger.warning("Pending write queue full — dropped 100 oldest", depth=depth)
            await self._db.insert("pending_writes", {
                "target_table": table,
                "payload": json.dumps(payload),
                "status": "pending",
            })
            logger.warning("Hub unreachable — write queued", depth=depth, error=str(exc))
            return None

    async def _handle_remote_write(self, payload: dict | None, peer_id: str) -> dict:
        """Hub receives and applies a write from a non-hub node."""
        if not self._local.is_hub:
            logger.warning("Received mesh_write but not hub", peer_id=peer_id[:8])
            return {"ok": False, "error": "not hub"}

        if not payload:
            return {"ok": False, "error": "empty payload"}

        table = payload.get("table", "")
        data = payload.get("data", {})
        if not table or not data:
            return {"ok": False, "error": "missing table or data"}

        if table not in _MESH_WRITABLE_TABLES:
            logger.warning("Rejected write to restricted table", table=table, peer_id=peer_id[:8])
            return {"ok": False, "error": "table not allowed"}

        peer_row = await self._db.fetch_one(
            "SELECT account_id FROM mesh_nodes WHERE id = ?", (peer_id,)
        )
        if not peer_row or peer_row["account_id"] != self._local.account_id:
            logger.warning("Rejected cross-account write", peer_id=peer_id[:8])
            return {"ok": False, "error": "account mismatch"}

        allowed_cols = _MESH_WRITABLE_COLUMNS.get(table, frozenset())
        filtered_data = {k: v for k, v in data.items() if k in allowed_cols}
        if not filtered_data:
            return {"ok": False, "error": "no writable columns in payload"}

        try:
            row_id = await self._db.insert(table, filtered_data)
            logger.debug("Applied mesh write", table=table, peer_id=peer_id[:8], row_id=row_id)
            return {"ok": True, "row_id": row_id}
        except Exception as exc:
            logger.error("Failed to apply mesh write", peer_id=peer_id[:8], error=str(exc), exc_info=True)
            return {"ok": False, "error": "internal error"}

    async def _replay_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self._sync_interval)

                if self._local.is_hub:
                    continue

                pending = await self._db.fetch_all(
                    "SELECT id, payload, retry_count FROM pending_writes "
                    "WHERE status = 'pending' ORDER BY id ASC LIMIT ?",
                    (REPLAY_BATCH,),
                )
                if not pending:
                    continue

                for row in pending:
                    try:
                        p = json.loads(row["payload"])
                        await self._transport.send_to_hub("mesh_write", p)
                        await self._db.execute(
                            "UPDATE pending_writes SET status = 'sent' WHERE id = ?",
                            (row["id"],),
                        )
                    except (ConnectionError, asyncio.TimeoutError):
                        new_count = row["retry_count"] + 1
                        if new_count >= MAX_RETRY:
                            await self._db.execute(
                                "UPDATE pending_writes SET status = 'failed', retry_count = ? WHERE id = ?",
                                (new_count, row["id"]),
                            )
                            logger.error("Pending write hit max retries", write_id=row["id"])
                        else:
                            await self._db.execute(
                                "UPDATE pending_writes SET retry_count = ? WHERE id = ?",
                                (new_count, row["id"]),
                            )
                        break
                    except Exception as exc:
                        logger.error("Replay error", write_id=row["id"], error=str(exc))
                        break

            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.error("Replay loop error", error=str(exc))
