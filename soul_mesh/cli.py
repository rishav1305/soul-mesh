"""Click-based CLI for soul-mesh.

Commands
--------
- ``soul-mesh init`` -- Initialize a new node (hub or agent)
- ``soul-mesh serve`` -- Start the hub server or agent heartbeat loop
- ``soul-mesh status`` -- Show cluster totals
- ``soul-mesh nodes`` -- List registered nodes
"""

from __future__ import annotations

import asyncio
import platform
import secrets
import uuid
from pathlib import Path

import click
import yaml

from soul_mesh.config import CONFIG_DIR, load_config
from soul_mesh.db import MeshDB
from soul_mesh.hub import Hub


@click.group()
def main():
    """soul-mesh -- distributed compute mesh for homelabbers."""


@main.command()
@click.option("--role", required=True, type=click.Choice(["hub", "agent"]), help="Node role.")
@click.option("--hub", "hub_address", default=None, help="Hub address (host:port). Required for agents.")
@click.option("--secret", default=None, help="Cluster secret. Auto-generated for hubs if omitted.")
@click.option("--name", default=None, help="Node name. Defaults to hostname.")
@click.option("--port", default=8340, type=int, help="Port for the mesh API.")
@click.option(
    "--config-dir",
    default=None,
    type=click.Path(),
    help="Config directory. Defaults to ~/.soul-mesh.",
)
def init(role: str, hub_address: str | None, secret: str | None, name: str | None, port: int, config_dir: str | None):
    """Initialize a new soul-mesh node."""
    # Validate: agents must specify a hub
    if role == "agent" and not hub_address:
        raise click.UsageError("Agents require --hub <host:port> to connect to a hub.")

    config_path = Path(config_dir) if config_dir else CONFIG_DIR
    config_path.mkdir(parents=True, exist_ok=True)

    # Generate secret for hubs if not provided
    if role == "hub" and not secret:
        secret = secrets.token_urlsafe(32)

    # Generate node ID
    node_id = str(uuid.uuid4())
    node_id_file = config_path / "node_id"
    node_id_file.write_text(node_id)

    # Resolve node name
    node_name = name or platform.node()

    # Build config dict
    config = {
        "node": {
            "name": node_name,
            "role": role,
            "port": port,
            "hub": hub_address or "",
            "heartbeat_interval": 10,
            "stale_timeout": 30,
        },
        "auth": {
            "secret": secret or "",
        },
        "discovery": {
            "mdns": True,
            "tailscale": False,
        },
    }

    config_file = config_path / "config.yaml"
    config_file.write_text(yaml.dump(config, default_flow_style=False, sort_keys=False))

    click.echo(f"Initialized {role} node: {node_name} ({node_id[:8]}...)")
    click.echo(f"Config written to {config_file}")

    if role == "hub":
        click.echo(f"\nTo add devices to this cluster, run on each agent:")
        click.echo(f"  soul-mesh init --role agent --hub <this-ip>:{port} --secret {secret}")
    else:
        click.echo(f"\nAgent configured to connect to hub at {hub_address}")
        click.echo(f"Start with: soul-mesh serve")


@main.command()
@click.option(
    "--config-dir",
    default=None,
    type=click.Path(),
    help="Config directory. Defaults to ~/.soul-mesh.",
)
def serve(config_dir: str | None):
    """Start the hub server or agent heartbeat loop."""
    config_path = Path(config_dir) if config_dir else CONFIG_DIR
    config_file = config_path / "config.yaml"

    if not config_file.exists():
        raise click.UsageError(f"No config found at {config_file}. Run 'soul-mesh init' first.")

    cfg = load_config(str(config_file))

    if cfg.role == "hub":
        _serve_hub(cfg, config_path)
    else:
        _serve_agent(cfg, config_path)


def _serve_hub(cfg, config_path: Path):
    """Start the hub with uvicorn."""
    from soul_mesh.node import NodeInfo
    from soul_mesh.server import create_app

    db_path = str(config_path / "mesh.db")
    db = MeshDB(db_path)

    # Ensure tables exist
    asyncio.run(db.ensure_tables())

    node = NodeInfo(
        node_name=cfg.name,
        port=cfg.port,
        node_id_path=str(config_path / "node_id"),
    )
    asyncio.run(node.init())

    app = create_app(db, node, secret=cfg.secret, stale_interval=cfg.stale_timeout)

    click.echo(f"Starting hub on port {cfg.port}...")

    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=cfg.port)


def _serve_agent(cfg, config_path: Path):
    """Start the agent heartbeat loop."""
    from soul_mesh.agent import Agent

    agent = Agent(cfg, node_id_path=str(config_path / "node_id"))

    async def _run():
        await agent.start()
        click.echo(f"Agent running, heartbeating to {cfg.hub}. Press Ctrl+C to stop.")
        try:
            while agent.running:
                await asyncio.sleep(1)
        except KeyboardInterrupt:
            pass
        finally:
            await agent.stop()

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        click.echo("\nAgent stopped.")


@main.command()
@click.option(
    "--config-dir",
    default=None,
    type=click.Path(),
    help="Config directory. Defaults to ~/.soul-mesh.",
)
def status(config_dir: str | None):
    """Show cluster status totals."""
    config_path = Path(config_dir) if config_dir else CONFIG_DIR
    config_file = config_path / "config.yaml"

    if not config_file.exists():
        raise click.UsageError(f"No config found at {config_file}. Run 'soul-mesh init' first.")

    cfg = load_config(str(config_file))
    db_path = str(config_path / "mesh.db")
    db = MeshDB(db_path)

    async def _status():
        await db.ensure_tables()
        hub = Hub(db)
        totals = await hub.cluster_totals()
        return totals

    totals = asyncio.run(_status())

    click.echo("Cluster Status")
    click.echo(f"  Nodes online:  {totals['nodes_online']}")
    click.echo(f"  CPU cores:     {totals['cpu_cores']}")
    click.echo(f"  RAM total:     {totals['ram_total_mb']} MB")
    click.echo(f"  Storage total: {totals['storage_total_gb']:.1f} GB")


@main.command()
@click.option(
    "--config-dir",
    default=None,
    type=click.Path(),
    help="Config directory. Defaults to ~/.soul-mesh.",
)
def nodes(config_dir: str | None):
    """List all registered nodes."""
    config_path = Path(config_dir) if config_dir else CONFIG_DIR
    config_file = config_path / "config.yaml"

    if not config_file.exists():
        raise click.UsageError(f"No config found at {config_file}. Run 'soul-mesh init' first.")

    cfg = load_config(str(config_file))
    db_path = str(config_path / "mesh.db")
    db = MeshDB(db_path)

    async def _nodes():
        await db.ensure_tables()
        hub = Hub(db)
        return await hub.list_nodes()

    node_list = asyncio.run(_nodes())

    if not node_list:
        click.echo("No nodes registered.")
        return

    click.echo(f"{'Name':<20} {'Status':<10} {'CPU':<6} {'RAM (MB)':<10} {'Storage (GB)':<12} {'ID'}")
    click.echo("-" * 80)
    for n in node_list:
        click.echo(
            f"{n.get('name', ''):<20} "
            f"{n.get('status', ''):<10} "
            f"{n.get('cpu_cores', 0):<6} "
            f"{n.get('ram_total_mb', 0):<10} "
            f"{n.get('storage_total_gb', 0):<12.1f} "
            f"{n.get('id', '')}"
        )
