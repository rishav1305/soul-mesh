"""Mesh node identity and capability scoring.

No psutil dependency: uses subprocess calls to ``free``/``df`` (same as
the healing module) and reads /sys/class/power_supply for battery state.
"""

from __future__ import annotations

import asyncio
import logging
import platform as _platform
import uuid as _uuid
from pathlib import Path

import structlog

logger = structlog.get_logger("soul-mesh.node")


class NodeInfo:
    """Local node identity with capability scoring.

    Works standalone without a database. Call ``init()`` to populate
    system info and generate a stable node ID (persisted to a local file).

    Parameters
    ----------
    node_name : str | None
        Human-readable node name. Defaults to the system hostname.
    port : int
        Port for the mesh API. Defaults to 8340.
    node_id_path : str | Path | None
        Path to a file storing the persistent node UUID.
        Defaults to ``~/.soul-mesh/node_id``.
        Pass ``":memory:"`` to skip file persistence (random UUID each run).
    """

    def __init__(
        self,
        node_name: str | None = None,
        port: int = 8340,
        node_id_path: str | Path | None = None,
    ) -> None:
        self.id: str = ""
        self.name: str = node_name or _platform.node()
        self.platform: str = _platform.system().lower()
        self.arch: str = _platform.machine()
        self.ram_mb: int = 0
        self.storage_mb: int = 0
        self.is_hub: bool = False
        self.status: str = "online"
        self.host: str = ""
        self.port: int = port
        self.account_id: str = ""
        self._battery_powered: bool = False
        self._node_id_path: Path | None = (
            None
            if node_id_path == ":memory:"
            else Path(node_id_path) if node_id_path else Path.home() / ".soul-mesh" / "node_id"
        )

    async def init(self) -> None:
        """Load (or create) the device UUID and populate system info."""
        self.id = self._load_or_create_id()
        self.ram_mb = await _get_ram_mb()
        self.storage_mb = await _get_storage_mb()
        self._battery_powered = await asyncio.to_thread(_is_battery_powered)

    def _load_or_create_id(self) -> str:
        """Return a stable UUID, persisting to file if configured."""
        if self._node_id_path is None:
            return str(_uuid.uuid4())

        try:
            if self._node_id_path.exists():
                stored = self._node_id_path.read_text().strip()
                if stored:
                    return stored
        except OSError as exc:
            logger.warning("Could not read node_id file", error=str(exc))

        new_id = str(_uuid.uuid4())
        try:
            self._node_id_path.parent.mkdir(parents=True, exist_ok=True)
            self._node_id_path.write_text(new_id)
            logger.info("First run: node_id assigned", node_id=new_id[:8])
        except OSError as exc:
            logger.warning("Could not persist node_id", error=str(exc))

        return new_id

    def capability_score(self) -> float:
        """Weighted additive score with capped components.

        - RAM:     up to 40 pts  (8 GiB = max)
        - Storage: up to 20 pts  (500 GiB = max)
        - Battery penalty: 50 % if on battery power
        """
        ram_score = min(self.ram_mb / 8192, 1.0) * 40
        storage_score = min(self.storage_mb / 512000, 1.0) * 20
        battery_penalty = 0.5 if self._battery_powered else 1.0
        return (ram_score + storage_score) * battery_penalty

    def to_dict(self) -> dict:
        """Serialize node info to a plain dict."""
        return {
            "id": self.id,
            "name": self.name,
            "host": self.host,
            "port": self.port,
            "platform": self.platform,
            "arch": self.arch,
            "ram_mb": self.ram_mb,
            "storage_mb": self.storage_mb,
            "is_hub": self.is_hub,
            "status": self.status,
            "capability": self.capability_score(),
            "account_id": self.account_id,
        }

    async def register(self, db_path: str | None = None) -> None:
        """Upsert this node in the ``mesh_nodes`` table.

        Parameters
        ----------
        db_path : str | None
            Path to the SQLite database. If None, registration is a no-op.
        """
        if db_path is None:
            return

        import aiosqlite

        async with aiosqlite.connect(db_path) as db:
            await db.execute(
                """CREATE TABLE IF NOT EXISTS mesh_nodes (
                    id TEXT PRIMARY KEY,
                    account_id TEXT DEFAULT '',
                    name TEXT DEFAULT '',
                    host TEXT DEFAULT '',
                    port INTEGER DEFAULT 8340,
                    platform TEXT DEFAULT '',
                    arch TEXT DEFAULT '',
                    ram_mb INTEGER DEFAULT 0,
                    storage_mb INTEGER DEFAULT 0,
                    is_hub INTEGER DEFAULT 0,
                    capability REAL DEFAULT 0.0,
                    last_seen TEXT DEFAULT '',
                    status TEXT DEFAULT 'online'
                )"""
            )
            await db.execute(
                """INSERT INTO mesh_nodes
                   (id, account_id, name, host, port, platform, arch,
                    ram_mb, storage_mb, is_hub, capability, last_seen, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                           strftime('%Y-%m-%dT%H:%M:%SZ', 'now'), 'online')
                   ON CONFLICT(id) DO UPDATE SET
                     name       = excluded.name,
                     host       = excluded.host,
                     port       = excluded.port,
                     ram_mb     = excluded.ram_mb,
                     storage_mb = excluded.storage_mb,
                     is_hub     = excluded.is_hub,
                     capability = excluded.capability,
                     last_seen  = excluded.last_seen,
                     status     = excluded.status
                """,
                (
                    self.id,
                    self.account_id,
                    self.name,
                    self.host,
                    self.port,
                    self.platform,
                    self.arch,
                    self.ram_mb,
                    self.storage_mb,
                    int(self.is_hub),
                    self.capability_score(),
                ),
            )
            await db.commit()


async def _get_ram_mb() -> int:
    """Total RAM in MiB via ``free -m`` (Linux) or ``sysctl`` (macOS)."""
    try:
        if _platform.system().lower() == "linux":
            proc = await asyncio.create_subprocess_exec(
                "free", "-m",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
            line = stdout.decode().splitlines()[1]
            return int(line.split()[1])
        proc = await asyncio.create_subprocess_exec(
            "sysctl", "-n", "hw.memsize",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        return int(stdout.decode().strip()) // (1024 * 1024)
    except Exception as exc:
        logger.warning("Could not determine RAM", error=str(exc))
        return 0


async def _get_storage_mb() -> int:
    """Total root-partition storage in MiB via ``df -m /``."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "df", "-m", "/",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        line = stdout.decode().splitlines()[1]
        return int(line.split()[1])
    except Exception as exc:
        logger.warning("Could not determine storage", error=str(exc))
        return 0


def _is_battery_powered() -> bool:
    """Check battery status via /sys (Linux only)."""
    try:
        ps_dir = Path("/sys/class/power_supply")
        if not ps_dir.exists():
            return False
        for supply in ps_dir.iterdir():
            type_file = supply / "type"
            if type_file.exists() and type_file.read_text().strip() == "Battery":
                status_file = supply / "status"
                if status_file.exists():
                    return status_file.read_text().strip() == "Discharging"
        return False
    except Exception as exc:
        logger.debug("Battery check failed", error=str(exc))
        return False
