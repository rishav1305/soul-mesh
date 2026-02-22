# soul-mesh

Multi-device mesh networking with hub election, peer discovery, and delta sync.

## Installation

```bash
pip install soul-mesh
```

For development:

```bash
pip install -e ".[dev]"
```

## Quick Start

```python
import asyncio
from soul_mesh.node import NodeInfo
from soul_mesh.election import elect_hub

# Create and initialize a node
async def main():
    node = NodeInfo(node_name="my-server", port=8080)
    await node.init()
    print(f"Node {node.name} capability: {node.capability_score():.1f}")

    # Run a pure-function election across known nodes
    nodes = [
        {"id": "aaa", "name": "server-1", "capability": 50.0, "is_hub": False},
        {"id": "bbb", "name": "server-2", "capability": 42.0, "is_hub": False},
    ]
    winner = elect_hub(nodes)
    print(f"Elected hub: {winner}")

asyncio.run(main())
```

## Architecture

- **Hub Election** -- The most capable node becomes the hub. A 20% hysteresis margin prevents flip-flopping: the current hub keeps its role unless a challenger exceeds its capability by at least 20%.
- **Peer Discovery** -- Discovers mesh peers via Tailscale (`tailscale status --json`) and probes each peer's identity endpoint. Peers are tracked in-memory with optional SQLite persistence.
- **Capability Scoring** -- Weighted additive score based on RAM (40 pts max), storage (20 pts max), with a 50% penalty for battery-powered devices.
- **Standalone** -- No dependency on soul-os. Works with constructor args, environment variables (`MESH_` prefix), or sensible defaults.

## Configuration

Environment variables (all optional):

| Variable | Default | Description |
|----------|---------|-------------|
| `MESH_NODE_NAME` | hostname | Human-readable node name |
| `MESH_PORT` | `8340` | Port for mesh API |
| `MESH_DISCOVERY_INTERVAL` | `30` | Seconds between discovery scans |
| `MESH_SHARED_SECRET` | `""` | Shared secret for HMAC peer verification |

## License

Apache-2.0
