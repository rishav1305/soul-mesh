"""Hub control plane -- node registry, heartbeats, resource aggregation.

The hub is the elected coordinator in a soul-mesh cluster.  It maintains
the authoritative node registry (stored in SQLite via MeshDB) and
aggregates resource snapshots from heartbeats into cluster-wide totals.
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta

import structlog

from soul_mesh.db import MeshDB

logger = structlog.get_logger("soul-mesh.hub")


class Hub:
    """Hub control plane for a soul-mesh cluster.

    Parameters
    ----------
    db : MeshDB
        Database handle (tables must already exist via ``ensure_tables``).
    """

    def __init__(self, db: MeshDB) -> None:
        self._db = db

    async def register_node(self, data: dict) -> None:
        """Register (or re-register) a node in the cluster.

        Parameters
        ----------
        data : dict
            Registration payload with keys:
            ``node_id``, ``name``, ``host``, ``port``, ``platform``,
            ``arch``, ``cpu`` (dict with ``cores``, ``usage_percent``,
            ``load_avg_1m``), ``memory`` (dict with ``total_mb``,
            ``available_mb``, ``used_percent``), ``storage`` (dict with
            ``mounts`` list of ``{path, total_gb, free_gb}``).
        """
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        cpu = data.get("cpu", {})
        memory = data.get("memory", {})
        storage = data.get("storage", {})
        storage_total_gb = sum(m.get("total_gb", 0) for m in storage.get("mounts", []))
        storage_free_gb = sum(m.get("free_gb", 0) for m in storage.get("mounts", []))

        # Upsert node record
        await self._db.upsert_node({
            "id": data["node_id"],
            "name": data.get("name", ""),
            "host": data.get("host", ""),
            "port": data.get("port", 8340),
            "platform": data.get("platform", ""),
            "arch": data.get("arch", ""),
            "cpu_cores": cpu.get("cores", 0),
            "ram_total_mb": memory.get("total_mb", 0),
            "storage_total_gb": storage_total_gb,
            "last_heartbeat": now,
        })

        # Set status to online
        await self._db.execute(
            "UPDATE nodes SET status = 'online' WHERE id = ?",
            (data["node_id"],),
        )

        # Insert initial heartbeat row
        await self._db.insert("heartbeats", {
            "node_id": data["node_id"],
            "cpu_usage_percent": cpu.get("usage_percent", 0.0),
            "cpu_load_1m": cpu.get("load_avg_1m", 0.0),
            "ram_available_mb": memory.get("available_mb", 0),
            "ram_used_percent": memory.get("used_percent", 0.0),
            "storage_free_gb": storage_free_gb,
        })

        logger.info(
            "node_registered",
            node_id=data["node_id"],
            name=data.get("name", ""),
            cpu_cores=cpu.get("cores", 0),
            ram_total_mb=memory.get("total_mb", 0),
        )

    async def process_heartbeat(self, node_id: str, snapshot: dict) -> None:
        """Process a heartbeat from a node.

        Updates the node's last_heartbeat timestamp and status to online,
        then inserts a new heartbeat row with the resource snapshot.

        Parameters
        ----------
        node_id : str
            The node sending the heartbeat.
        snapshot : dict
            Resource snapshot with ``cpu`` (``usage_percent``,
            ``load_avg_1m``), ``memory`` (``available_mb``,
            ``used_percent``), ``storage`` (``mounts`` list).
        """
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        cpu = snapshot.get("cpu", {})
        memory = snapshot.get("memory", {})
        storage = snapshot.get("storage", {})
        storage_free_gb = sum(m.get("free_gb", 0) for m in storage.get("mounts", []))

        # Update node timestamp and status
        await self._db.execute(
            "UPDATE nodes SET last_heartbeat = ?, status = 'online' WHERE id = ?",
            (now, node_id),
        )

        # Insert heartbeat row
        await self._db.insert("heartbeats", {
            "node_id": node_id,
            "cpu_usage_percent": cpu.get("usage_percent", 0.0),
            "cpu_load_1m": cpu.get("load_avg_1m", 0.0),
            "ram_available_mb": memory.get("available_mb", 0),
            "ram_used_percent": memory.get("used_percent", 0.0),
            "storage_free_gb": storage_free_gb,
        })

        logger.debug("heartbeat_processed", node_id=node_id)

    async def mark_stale_nodes(self, timeout_seconds: int = 30) -> list[str]:
        """Find and mark nodes whose last heartbeat is older than the timeout.

        Parameters
        ----------
        timeout_seconds : int
            Number of seconds after which a node is considered stale.

        Returns
        -------
        list[str]
            IDs of nodes that were marked stale.
        """
        cutoff = (
            datetime.now(timezone.utc) - timedelta(seconds=timeout_seconds)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")

        stale_rows = await self._db.fetch_all(
            "SELECT id FROM nodes WHERE status = 'online' AND last_heartbeat < ? AND last_heartbeat != ''",
            (cutoff,),
        )
        stale_ids = [row["id"] for row in stale_rows]

        if stale_ids:
            placeholders = ", ".join("?" for _ in stale_ids)
            await self._db.execute(
                f"UPDATE nodes SET status = 'stale' WHERE id IN ({placeholders})",
                tuple(stale_ids),
            )
            logger.info("stale_nodes_marked", count=len(stale_ids), node_ids=stale_ids)

        return stale_ids

    async def cluster_totals(self) -> dict:
        """Aggregate resources across all online nodes.

        Returns
        -------
        dict
            ``nodes_online`` (int), ``cpu_cores`` (int),
            ``ram_total_mb`` (int), ``storage_total_gb`` (float).
        """
        row = await self._db.fetch_one(
            """SELECT
                COUNT(*) AS nodes_online,
                COALESCE(SUM(cpu_cores), 0) AS cpu_cores,
                COALESCE(SUM(ram_total_mb), 0) AS ram_total_mb,
                COALESCE(SUM(storage_total_gb), 0.0) AS storage_total_gb
            FROM nodes
            WHERE status = 'online'"""
        )
        if row is None:
            return {
                "nodes_online": 0,
                "cpu_cores": 0,
                "ram_total_mb": 0,
                "storage_total_gb": 0.0,
            }
        return {
            "nodes_online": row["nodes_online"],
            "cpu_cores": row["cpu_cores"],
            "ram_total_mb": row["ram_total_mb"],
            "storage_total_gb": float(row["storage_total_gb"]),
        }

    async def heartbeat_history(self, node_id: str, limit: int = 30) -> list[dict]:
        """Return recent heartbeats for a node, most recent first.

        Parameters
        ----------
        node_id : str
            The node whose heartbeat history to retrieve.
        limit : int
            Maximum number of heartbeat rows to return (default 30).

        Returns
        -------
        list[dict]
            Heartbeat rows ordered by ``recorded_at`` descending.
        """
        limit = max(1, limit)
        return await self._db.fetch_all(
            "SELECT * FROM heartbeats WHERE node_id = ? ORDER BY recorded_at DESC LIMIT ?",
            (node_id, limit),
        )

    async def list_nodes(self) -> list[dict]:
        """Return all registered nodes ordered by name.

        Returns
        -------
        list[dict]
            All columns from the nodes table, ordered alphabetically by name.
        """
        return await self._db.fetch_all("SELECT * FROM nodes ORDER BY name")
