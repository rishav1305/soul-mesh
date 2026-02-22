# soul-mesh

Distributed compute mesh for homelabbers -- unified RAM, storage, and CPU across devices.

Soul-mesh connects Raspberry Pis, laptops, desktops, and servers into a single cluster. A manually designated **hub** acts as the control plane, while **agent** nodes report live CPU, RAM, and disk metrics via WebSocket heartbeats. The hub aggregates resources across the cluster so you can see and manage all devices as one machine.

## Quick Start

```bash
pip install soul-mesh[server]
```

Initialize the hub (one device):

```bash
soul-mesh init --role hub --name my-server
```

Initialize agents (every other device):

```bash
soul-mesh init --role agent --hub 192.168.1.10:8340 --secret <secret-from-hub>
```

Start the mesh:

```bash
soul-mesh serve   # on every device
soul-mesh status  # cluster totals
soul-mesh nodes   # list all devices
```

Requires Python 3.11+.

## Architecture

```
                       +------------------+
                       |   PeerDiscovery  |
                       | (mDNS / Tailscale)|
                       +--------+---------+
                                |
                                v
+----------+          +--------+---------+          +-----------+
| NodeInfo +--------->|   HubElection    |          |  MeshDB   |
| (scoring)|          | (20% hysteresis) |          | (SQLite)  |
+----------+          +--------+---------+          +-----+-----+
                                |                         |
                     elects hub |                         |
                                v                         |
                       +--------+---------+               |
                       |  MeshTransport   +<--------------+
                       |  (WebSocket +    |
                       |   JWT auth)      |
                       +--------+---------+
                                |
                    +-----------+-----------+
                    |           |           |
             +------+---+ +----+----+ +----+----+
             |   Hub    | |  Agent  | | Linking |
             | (control | | (heart- | | (device |
             |  plane)  | |  beat)  | |  pair)  |
             +----------+ +---------+ +---------+
```

### Data flow

1. **Discovery** finds mesh peers on the LAN via mDNS (Tailscale optional)
2. **Election** scores each node by hardware capability and elects the best one as hub
3. **Transport** opens authenticated WebSocket connections between nodes
4. **Hub** maintains the node registry and aggregates cluster resources from heartbeats
5. **Agent** sends live CPU/RAM/disk snapshots to the hub every 10 seconds
6. **Linking** pairs new devices to the same account via short-lived codes

### Design principles

- **Hub-agent model** -- one hub runs the control plane, agents report resources via heartbeats
- **Dependency injection** -- every module takes its dependencies as constructor parameters
- **Standalone** -- zero imports from soul-os, works independently with `pip install`
- **Security boundaries** -- table/column allowlists for SQL, JWT auth for transport, rate limiting for linking
- **No psutil** -- resources collected via `/proc/meminfo`, `os.getloadavg()`, `df` subprocess

## Modules

### MeshConfig (`soul_mesh.config`)

YAML configuration with environment variable overrides.

```python
from soul_mesh import MeshConfig, load_config

# Load from ~/.soul-mesh/config.yaml with MESH_* env overrides
cfg = load_config()

# Or build programmatically
cfg = MeshConfig(name="my-pi", role="hub", port=8340, secret="my-secret")
```

Config file format (`~/.soul-mesh/config.yaml`):

```yaml
node:
  name: titan-pc
  role: hub
  port: 8340
  heartbeat_interval: 10
  stale_timeout: 30

auth:
  secret: my-cluster-secret

discovery:
  mdns: true
  tailscale: false
```

### Hub (`soul_mesh.hub`)

Hub control plane -- node registry, heartbeats, resource aggregation.

```python
from soul_mesh import Hub, MeshDB

db = MeshDB(":memory:")
await db.ensure_tables()
hub = Hub(db)

# Register a node (from a heartbeat payload)
await hub.register_node({"node_id": "abc", "name": "pi-4", "cpu": {"cores": 4}, ...})

# Process subsequent heartbeats
await hub.process_heartbeat("abc", {"cpu": {...}, "memory": {...}, "storage": {...}})

# Check cluster totals
totals = await hub.cluster_totals()
# {"nodes_online": 3, "cpu_cores": 12, "ram_total_mb": 16384, "storage_total_gb": 1200.0}

# Mark stale nodes (no heartbeat in 30s)
stale_ids = await hub.mark_stale_nodes(timeout_seconds=30)
```

### Agent (`soul_mesh.agent`)

Agent heartbeat loop -- connects to the hub and reports resource snapshots.

```python
from soul_mesh import Agent, MeshConfig

cfg = MeshConfig(name="my-pi", role="agent", hub="192.168.1.10:8340", secret="s")
agent = Agent(cfg)

await agent.start()   # connects to hub, begins heartbeating
await agent.stop()    # stops heartbeat loop, closes connection
```

The agent uses exponential backoff (1s to 60s) when the hub is unreachable.

### Resources (`soul_mesh.resources`)

Live system resource collection without psutil.

```python
from soul_mesh.resources import get_system_snapshot

snapshot = await get_system_snapshot()
# {
#   "cpu": {"cores": 4, "usage_percent": 23.5, "load_avg_1m": 0.94},
#   "memory": {"total_mb": 8192, "available_mb": 4096, "used_percent": 50.0},
#   "storage": {"mounts": [{"path": "/", "total_gb": 500, "free_gb": 200}]}
# }
```

Supports Linux (`/proc/meminfo`) and macOS (`sysctl`).

### Server (`soul_mesh.server`)

FastAPI REST endpoints for the hub. Optional dependency (`pip install soul-mesh[server]`).

```python
from soul_mesh.server import create_app
from soul_mesh.db import MeshDB

db = MeshDB("mesh.db")
await db.ensure_tables()
app = create_app(db)

# Endpoints:
# GET /api/mesh/health    -> {"status": "ok"}
# GET /api/mesh/status    -> cluster totals
# GET /api/mesh/nodes     -> list of all nodes
# GET /api/mesh/identity  -> this node's info
```

### NodeInfo (`soul_mesh.node`)

Local device identity and hardware capability scoring.

```python
from soul_mesh import NodeInfo

node = NodeInfo(node_name="my-pi", port=8340)
await node.init()  # reads RAM, storage, battery status

print(node.id)                # persistent UUID
print(node.capability_score())  # 0-60 weighted score
```

**Capability scoring:** RAM (up to 40 pts), Storage (up to 20 pts), battery penalty (50% reduction).

Node IDs persist across restarts in `~/.soul-mesh/node_id`. Pass `node_id_path=":memory:"` for ephemeral IDs.

### MeshDB (`soul_mesh.db`)

Thin async SQLite wrapper used by all modules.

```python
from soul_mesh import MeshDB

db = MeshDB(":memory:")  # or a file path
await db.ensure_tables()

# CRUD
await db.insert("settings", {"key": "theme", "value": "dark"})
row = await db.fetch_one("SELECT value FROM settings WHERE key = ?", ("theme",))
rows = await db.fetch_all("SELECT * FROM nodes WHERE status = 'online'")

# Upsert nodes
await db.upsert_node({"id": "abc", "name": "pi", "host": "10.0.0.1", ...})

# Transactions
async with db.transaction() as cursor:
    await cursor.execute("INSERT INTO link_attempts ...")
    await cursor.execute("UPDATE settings ...")
    # auto-commits on success, rolls back on exception
```

**Tables:** `nodes`, `heartbeats`, `link_codes`, `link_attempts`, `settings`

**Security:** `insert()` validates table names against a frozen allowlist. All queries use parameterized `?` placeholders.

### HubElection (`soul_mesh.election`)

Elects the most capable node as hub with hysteresis to prevent flip-flopping.

```python
from soul_mesh import elect_hub

nodes = [
    {"id": "aaa", "name": "pi-4", "capability": 35.0, "is_hub": True},
    {"id": "bbb", "name": "desktop", "capability": 55.0, "is_hub": False},
]
winner = elect_hub(nodes)  # "bbb" wins (55 > 35 * 1.2)
```

The current hub keeps its role unless a challenger exceeds its capability by more than 20% (the hysteresis margin).

### Auth (`soul_mesh.auth`)

JWT tokens for authenticating WebSocket connections between nodes.

```python
from soul_mesh import create_mesh_token, verify_mesh_token

token = create_mesh_token(
    node_id="abc-123",
    account_id="acct-456",
    secret="your-32-byte-secret-key-here!!!!!",
    ttl=3600,
)

claims = verify_mesh_token(token, secret="your-32-byte-secret-key-here!!!!!")
```

Uses HS256. Raises `jwt.ExpiredSignatureError` or `jwt.InvalidSignatureError` on failure.

### MeshTransport (`soul_mesh.transport`)

Authenticated WebSocket mesh communication with automatic reconnection.

```python
from soul_mesh import MeshTransport

transport = MeshTransport(local_node=node, db=db, secret="your-secret")
transport.on("chat", handle_chat_message)
await transport.start()

await transport.send(peer_id, "chat", {"text": "hello"})
await transport.broadcast("status_update", {"status": "online"})
```

Features: exponential backoff reconnection (1s-5min), 1 MiB message limit, 30s heartbeat, JSON message routing.

### PeerDiscovery (`soul_mesh.discovery`)

Discovers mesh peers via Tailscale with HMAC nonce verification.

```python
from soul_mesh.discovery import PeerDiscovery

discovery = PeerDiscovery(local_node=node, shared_secret="my-secret")
await discovery.start()
peers = discovery.get_online_peers()
```

### Linking (`soul_mesh.linking`)

Device pairing through rate-limited link codes.

```python
from soul_mesh import generate_link_code, redeem_link_code, get_or_create_account_id

# On the existing device
account_id = await get_or_create_account_id(db)
code = await generate_link_code(db, account_id)

# On the new device
result = await redeem_link_code(db, code, node_id="new-node-id", ip_address="192.168.1.5")
```

**Security:** codes expire in 10 minutes, per-IP limit of 5 attempts per 15 minutes, global limit of 50 attempts per 15 minutes.

## CLI

```
soul-mesh init    Initialize a new node (hub or agent)
soul-mesh serve   Start the hub server or agent heartbeat loop
soul-mesh status  Show cluster resource totals
soul-mesh nodes   List all registered nodes
```

## Configuration

### Config file (`~/.soul-mesh/config.yaml`)

Generated by `soul-mesh init`. See the MeshConfig section above for format.

### Environment variables

All optional, override config file values:

| Variable | Default | Description |
|----------|---------|-------------|
| `MESH_ROLE` | `agent` | Node role: `hub` or `agent` |
| `MESH_PORT` | `8340` | Port for mesh API |
| `MESH_HUB` | `""` | Hub address (`host:port`) |
| `MESH_SECRET` | `""` | Shared cluster secret for JWT auth |

## Development

```bash
git clone <repo-url> && cd soul-mesh
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,server]"
pytest -v
```

179 tests across 11 test files:

| Test file | Tests | Covers |
|-----------|-------|--------|
| test_config.py | 24 | YAML loading, env overrides, defaults |
| test_resources.py | 20 | CPU, memory, storage collection |
| test_transport.py | 18 | WebSocket connections, message routing |
| test_db.py | 17 | CRUD, insert allowlist, transactions, upsert |
| test_hub.py | 16 | Node registry, heartbeats, stale detection, aggregation |
| test_election.py | 15 | Hub election, hysteresis, tie-breaking |
| test_linking.py | 14 | Code generation, redemption, rate limiting |
| test_agent.py | 13 | Heartbeat building, start/stop lifecycle |
| test_auth.py | 8 | JWT creation, verification, expiry |
| test_server.py | 7 | REST endpoints (health, status, nodes, identity) |
| test_cli.py | 7 | init, status, nodes commands |

## Roadmap

Soul-mesh is built in layers, each independently useful:

| Layer | What | Status |
|-------|------|--------|
| 0 | Mesh Core (discovery, auth, transport, heartbeats) | Shipped (v0.2.0) |
| 1 | Server (FastAPI REST API, CLI) | Shipped (v0.2.0) |
| 2 | Task Engine (distribute work across nodes) | Planned |
| 3 | Storage (unified filesystem across nodes) | Planned |
| 4 | Dashboard (web UI for cluster management) | Planned |

## License

Apache-2.0
