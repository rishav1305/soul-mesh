"""Hub election with hysteresis -- prevents flip-flopping.

The most capable node becomes the hub.  A 20% hysteresis margin
means the current hub keeps its role unless a challenger exceeds
its capability by at least 20%.
"""

from __future__ import annotations

import structlog

logger = structlog.get_logger("soul-mesh.election")

HYSTERESIS_MARGIN = 0.20


def elect_hub(
    nodes: list[dict],
    current_hub_id: str | None = None,
    hysteresis: float = HYSTERESIS_MARGIN,
) -> str:
    """Pure function: given a list of nodes, return the winner's id.

    Each node dict must have at minimum: ``id``, ``name``, ``capability``.
    Optionally ``is_hub`` (bool) to identify the current hub.

    Parameters
    ----------
    nodes : list[dict]
        List of node dicts, each with "id", "name", "capability",
        and optionally "is_hub".
    current_hub_id : str | None
        Explicit current hub ID. If None, inferred from ``is_hub`` field.
    hysteresis : float
        Fraction above current hub's capability required to dethrone it.
        Defaults to 0.20 (20%).

    Returns
    -------
    str
        The ``id`` of the winning node.

    Raises
    ------
    ValueError
        If the node list is empty.
    """
    if not nodes:
        raise ValueError("Cannot elect a hub from an empty node list")

    # Determine current hub
    hub_id = current_hub_id
    if hub_id is None:
        for n in nodes:
            if n.get("is_hub"):
                hub_id = n["id"]
                break

    current_hub = None
    if hub_id:
        current_hub = next((n for n in nodes if n["id"] == hub_id), None)

    # Sort: highest capability first, then by name, then by id (tie-break)
    candidates = sorted(
        nodes, key=lambda n: (-n["capability"], n.get("name", ""), n["id"])
    )
    top = candidates[0]

    if current_hub and current_hub["id"] != top["id"]:
        threshold = current_hub["capability"] * (1 + hysteresis)
        if top["capability"] > threshold:
            return top["id"]
        return current_hub["id"]

    return top["id"]


class HubElection:
    """Stateful hub election manager.

    Wraps the pure ``elect_hub`` function with local-node awareness
    and optional database persistence.

    Parameters
    ----------
    local_node : NodeInfo
        The local node instance.
    """

    def __init__(self, local_node) -> None:
        self._local = local_node

    def run(self, all_nodes: list[dict]) -> str:
        """Run hub election over a list of node dicts.

        Updates ``self._local.is_hub`` based on the result.

        Parameters
        ----------
        all_nodes : list[dict]
            All known online nodes (including local). Each dict must
            have "id", "name", "capability", and optionally "is_hub".

        Returns
        -------
        str
            The winning node's id.
        """
        if not all_nodes:
            self._local.is_hub = True
            logger.info(
                "No peers -- local node becomes hub",
                capability=self._local.capability_score(),
            )
            return self._local.id

        winner_id = elect_hub(all_nodes)

        was_hub = self._local.is_hub
        self._local.is_hub = winner_id == self._local.id

        if was_hub != self._local.is_hub:
            if self._local.is_hub:
                logger.info(
                    "This node elected as hub",
                    capability=self._local.capability_score(),
                )
            else:
                logger.info("Hub role transferred", new_hub=winner_id[:8])

        return winner_id

    async def elect(self, db=None) -> str:
        """Run election with optional MeshDB persistence.

        Parameters
        ----------
        db : MeshDB | None
            Database instance. If None, uses only the local node.
        """
        if db is None:
            self._local.is_hub = True
            return self._local.id

        rows = await db.fetch_all(
            "SELECT id, name, role, ram_total_mb, storage_total_gb "
            "FROM nodes WHERE status = 'online'"
        )

        if not rows:
            self._local.is_hub = True
            return self._local.id

        all_nodes = []
        for row in rows:
            ram = row.get("ram_total_mb", 0)
            storage = row.get("storage_total_gb", 0)
            cap = min(ram / 8192, 1.0) * 40 + min(storage / 500, 1.0) * 20
            all_nodes.append({
                "id": row["id"],
                "name": row.get("name", ""),
                "capability": round(cap, 2),
                "is_hub": row.get("role") == "hub",
            })

        winner_id = elect_hub(all_nodes)

        async with db.transaction() as cursor:
            await cursor.execute(
                "UPDATE nodes SET role = 'agent' WHERE role = 'hub' AND id != ?",
                (winner_id,),
            )
            await cursor.execute(
                "UPDATE nodes SET role = 'hub' WHERE id = ?",
                (winner_id,),
            )

        was_hub = self._local.is_hub
        self._local.is_hub = winner_id == self._local.id

        if was_hub != self._local.is_hub:
            if self._local.is_hub:
                logger.info(
                    "This node elected as hub",
                    capability=self._local.capability_score(),
                )
            else:
                logger.info("Hub role transferred", new_hub=winner_id[:8])

        return winner_id
