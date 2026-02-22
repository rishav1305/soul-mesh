"""Peer discovery via Tailscale -- no mDNS, no new dependencies.

Discovers mesh peers by querying ``tailscale status --json`` and probing
``/api/mesh/identity`` on each peer.  Discovered peers are tracked
in-memory with optional SQLite persistence.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import secrets

import structlog

logger = structlog.get_logger("soul-mesh.discovery")

_PROBE_TIMEOUT = 5


async def get_tailscale_status() -> dict:
    """Query ``tailscale status --json`` and return parsed result.

    Returns a dict with keys:
    - connected (bool)
    - tailscale_ip (str)
    - peers (list[dict]) -- each with "ip", "name", "online"
    """
    result: dict = {"connected": False, "tailscale_ip": "", "peers": []}
    try:
        proc = await asyncio.create_subprocess_exec(
            "tailscale", "status", "--json",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        if proc.returncode != 0:
            return result

        data = json.loads(stdout.decode())
        self_node = data.get("Self", {})
        ts_ips = self_node.get("TailscaleIPs", [])
        result["tailscale_ip"] = ts_ips[0] if ts_ips else ""
        result["connected"] = bool(self_node.get("Online", False))

        for peer_key, peer_data in data.get("Peer", {}).items():
            peer_ips = peer_data.get("TailscaleIPs", [])
            result["peers"].append({
                "ip": peer_ips[0] if peer_ips else "",
                "name": peer_data.get("HostName", ""),
                "online": peer_data.get("Online", False),
            })
    except FileNotFoundError:
        logger.debug("Tailscale CLI not found")
    except Exception as exc:
        logger.warning("Failed to get tailscale status", error=str(exc))

    return result


class PeerDiscovery:
    """Discovers mesh peers via the Tailscale network.

    Parameters
    ----------
    local_node : NodeInfo
        The local node instance.
    discovery_interval : int
        Seconds between discovery scans. Defaults to 30.
    shared_secret : str
        Shared secret for HMAC nonce verification. Empty string disables.
    """

    def __init__(
        self,
        local_node,
        discovery_interval: int = 30,
        shared_secret: str = "",
    ) -> None:
        self._local_node = local_node
        self._discovery_interval = discovery_interval
        self._shared_secret = shared_secret
        self._running = False
        self._task: asyncio.Task | None = None
        self._probe_tasks: set[asyncio.Task] = set()
        self._peers: dict[str, dict] = {}

    @property
    def peers(self) -> dict[str, dict]:
        """Return a copy of the in-memory peer registry."""
        return dict(self._peers)

    async def start(self) -> None:
        """Start the background discovery loop."""
        self._running = True
        self._task = asyncio.create_task(self._discovery_loop())
        logger.info(
            "Peer discovery started",
            interval=self._discovery_interval,
        )

    async def stop(self) -> None:
        """Stop the discovery loop and cancel pending probes."""
        self._running = False
        for task in self._probe_tasks:
            task.cancel()
        self._probe_tasks.clear()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Peer discovery stopped")

    async def _discovery_loop(self) -> None:
        while self._running:
            try:
                await self._scan_peers()
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.error("Discovery scan failed", error=str(exc), exc_info=True)
            await asyncio.sleep(self._discovery_interval)

    async def _scan_peers(self) -> None:
        ts = await get_tailscale_status()
        if not ts["connected"]:
            logger.debug("Tailscale not connected -- skipping scan")
            return

        if ts["tailscale_ip"]:
            self._local_node.host = ts["tailscale_ip"]

        for peer in ts["peers"]:
            if not peer.get("online") or not peer.get("ip"):
                continue
            task = asyncio.create_task(
                self._probe_peer(peer["ip"], peer.get("name", ""))
            )
            self._probe_tasks.add(task)
            task.add_done_callback(self._probe_tasks.discard)

    def _compute_capability(self, ram_mb: int, storage_mb: int) -> float:
        """Compute capability score locally from hardware specs.

        Uses the same formula as NodeInfo.capability_score() -- never trust
        self-reported scores from peers.
        """
        ram_score = min(ram_mb / 8192, 1.0) * 40
        storage_score = min(storage_mb / 512000, 1.0) * 20
        return round(ram_score + storage_score, 2)

    async def _probe_peer(self, ip: str, ts_name: str) -> None:
        """Probe a peer's identity endpoint and register it."""
        try:
            _create_sp = asyncio.create_subprocess_exec
            proc = await _create_sp(
                "curl", "-sf", "--max-time", str(_PROBE_TIMEOUT),
                f"http://{ip}:{self._local_node.port}/api/mesh/identity",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(
                proc.communicate(), timeout=_PROBE_TIMEOUT + 2
            )
            if proc.returncode != 0:
                return

            identity = json.loads(stdout.decode())
            peer_id = identity.get("node_id")
            if not peer_id or peer_id == self._local_node.id:
                return

            # Authenticated probe with HMAC nonce challenge
            nonce = secrets.token_hex(16)
            headers = ["-H", f"X-Mesh-Node: {self._local_node.id}"]
            proc2 = await _create_sp(
                "curl", "-sf", "--max-time", str(_PROBE_TIMEOUT),
                *headers,
                f"http://{ip}:{self._local_node.port}/api/mesh/probe?nonce={nonce}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout2, _ = await asyncio.wait_for(
                proc2.communicate(), timeout=_PROBE_TIMEOUT + 2
            )
            if proc2.returncode != 0:
                logger.debug("Authenticated probe failed", ip=ip)
                return

            data = json.loads(stdout2.decode())

            # Verify HMAC nonce signature if shared_secret is configured
            if self._shared_secret:
                peer_sig = data.get("nonce_sig", "")
                if peer_sig:
                    expected = hmac.new(
                        self._shared_secret.encode(),
                        nonce.encode(),
                        hashlib.sha256,
                    ).hexdigest()
                    if not hmac.compare_digest(peer_sig, expected):
                        logger.warning(
                            "HMAC nonce mismatch -- ignoring peer",
                            peer_id=peer_id[:8],
                        )
                        return

            if data.get("account_id") != self._local_node.account_id:
                logger.debug(
                    "Peer belongs to different account -- ignoring",
                    peer_id=peer_id[:8],
                )
                return

            ram_mb = data.get("ram_mb", 0)
            storage_mb = data.get("storage_mb", 0)
            computed_capability = self._compute_capability(ram_mb, storage_mb)

            peer_record = {
                "id": peer_id,
                "account_id": data.get("account_id", ""),
                "name": data.get("name", ts_name),
                "host": ip,
                "port": data.get("port", self._local_node.port),
                "platform": data.get("platform", ""),
                "arch": data.get("arch", ""),
                "ram_mb": ram_mb,
                "storage_mb": storage_mb,
                "capability": computed_capability,
                "status": "online",
                "is_hub": False,
            }
            self._peers[peer_id] = peer_record

            logger.info(
                "Discovered peer",
                name=peer_record["name"],
                peer_id=peer_id[:8],
                ip=ip,
            )

        except asyncio.TimeoutError:
            logger.debug("Probe timed out", ip=ip)
        except asyncio.CancelledError:
            return
        except Exception as exc:
            logger.warning("Probe failed", ip=ip, error=str(exc))

    def get_online_peers(self) -> list[dict]:
        """Return all known online peers from the in-memory registry."""
        return [p for p in self._peers.values() if p.get("status") == "online"]

    def mark_peer_offline(self, peer_id: str) -> None:
        """Mark a peer as offline in the in-memory registry."""
        if peer_id in self._peers:
            self._peers[peer_id]["status"] = "offline"
