"""YAML config loader with env var overrides.

Configuration is loaded from ``~/.soul-mesh/config.yaml`` by default.
Environment variables prefixed with ``MESH_`` override any file values.

Example config.yaml::

    node:
      name: titan-pc
      role: hub
      port: 8340
      hub: ""
      heartbeat_interval: 10
      stale_timeout: 30

    auth:
      secret: my-shared-secret

    discovery:
      mdns: true
      tailscale: false
"""

from __future__ import annotations

import os
import platform
from dataclasses import dataclass, replace
from pathlib import Path

import structlog
import yaml

logger = structlog.get_logger("soul-mesh.config")

CONFIG_DIR: Path = Path.home() / ".soul-mesh"
DEFAULT_CONFIG_PATH: Path = CONFIG_DIR / "config.yaml"


@dataclass
class MeshConfig:
    """Mesh node configuration with sensible defaults.

    Parameters
    ----------
    name : str
        Human-readable node name. Defaults to the system hostname.
    role : str
        Node role: ``"hub"`` or ``"agent"``. Defaults to ``"agent"``.
    port : int
        Port for the mesh API. Defaults to 8340.
    hub : str
        Address of the hub node (``host:port``). Empty for hub nodes.
    secret : str
        Shared secret for JWT authentication. Empty disables auth.
    mdns : bool
        Enable mDNS discovery. Defaults to True.
    tailscale : bool
        Enable Tailscale discovery. Defaults to False.
    heartbeat_interval : int
        Seconds between heartbeat pings. Defaults to 10.
    stale_timeout : int
        Seconds before a node is considered stale. Defaults to 30.
    """

    name: str = ""
    role: str = "agent"
    port: int = 8340
    hub: str = ""
    secret: str = ""
    mdns: bool = True
    tailscale: bool = False
    heartbeat_interval: int = 10
    stale_timeout: int = 30

    def __post_init__(self) -> None:
        if not self.name:
            self.name = platform.node()

    @classmethod
    def from_dict(cls, data: dict) -> MeshConfig:
        """Parse a nested YAML dict into a MeshConfig.

        Expected structure::

            node:
              name: ...
              role: ...
              port: ...
              hub: ...
              heartbeat_interval: ...
              stale_timeout: ...
            auth:
              secret: ...
            discovery:
              mdns: ...
              tailscale: ...
        """
        node = data.get("node", {})
        auth = data.get("auth", {})
        discovery = data.get("discovery", {})

        kwargs: dict = {}

        # node section
        if "name" in node:
            kwargs["name"] = node["name"]
        if "role" in node:
            kwargs["role"] = node["role"]
        if "port" in node:
            kwargs["port"] = int(node["port"])
        if "hub" in node:
            kwargs["hub"] = node["hub"]
        if "heartbeat_interval" in node:
            kwargs["heartbeat_interval"] = int(node["heartbeat_interval"])
        if "stale_timeout" in node:
            kwargs["stale_timeout"] = int(node["stale_timeout"])

        # auth section
        if "secret" in auth:
            kwargs["secret"] = auth["secret"]

        # discovery section
        if "mdns" in discovery:
            kwargs["mdns"] = bool(discovery["mdns"])
        if "tailscale" in discovery:
            kwargs["tailscale"] = bool(discovery["tailscale"])

        return cls(**kwargs)

    def with_env_overrides(self) -> MeshConfig:
        """Return a new config with MESH_* env vars applied.

        Supported environment variables:

        - ``MESH_ROLE`` -- overrides ``role``
        - ``MESH_PORT`` -- overrides ``port``
        - ``MESH_HUB`` -- overrides ``hub``
        - ``MESH_SECRET`` -- overrides ``secret``
        """
        overrides: dict = {}

        role = os.environ.get("MESH_ROLE")
        if role is not None:
            overrides["role"] = role

        port = os.environ.get("MESH_PORT")
        if port is not None:
            overrides["port"] = int(port)

        hub = os.environ.get("MESH_HUB")
        if hub is not None:
            overrides["hub"] = hub

        secret = os.environ.get("MESH_SECRET")
        if secret is not None:
            overrides["secret"] = secret

        if overrides:
            return replace(self, **overrides)
        return replace(self)


def load_config(path: Path | str | None = None) -> MeshConfig:
    """Load config from a YAML file, then apply env var overrides.

    Parameters
    ----------
    path : Path | str | None
        Path to the YAML config file. Defaults to
        ``~/.soul-mesh/config.yaml``. If the file does not exist,
        returns defaults with env overrides applied.
    """
    config_path = Path(path) if path else DEFAULT_CONFIG_PATH

    if config_path.exists():
        try:
            raw = yaml.safe_load(config_path.read_text()) or {}
            logger.info("Config loaded", path=str(config_path))
            cfg = MeshConfig.from_dict(raw)
        except Exception as exc:
            logger.warning("Failed to parse config, using defaults", path=str(config_path), error=str(exc))
            cfg = MeshConfig()
    else:
        logger.debug("Config file not found, using defaults", path=str(config_path))
        cfg = MeshConfig()

    return cfg.with_env_overrides()
