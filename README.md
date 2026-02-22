# soul-mesh

Multi-device mesh networking with hub election, WebSocket transport, and offline-tolerant sync.

Soul-mesh lets multiple devices (Raspberry Pi, desktop, laptop) form a self-organizing network. One node is elected as the **hub** and acts as the central data authority. Other nodes forward writes to the hub over WebSocket, with automatic offline queueing when connectivity drops. New devices join the mesh through rate-limited link codes.

## Installation

```bash
pip install soul-mesh
```

For development:

```bash
git clone <repo-url> && cd soul-mesh
pip install -e ".[dev]"
pytest
```

Requires Python 3.11+.

## Architecture

```
                       +------------------+
                       |   PeerDiscovery  |
                       | (Tailscale scan) |
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
                    |                       |
             +------+------+        +------+------+
             |  MeshSync   |        |   Linking   |
             | (hub-proxy  |        | (link codes |
             |  + offline  |        |  + rate     |
             |  queue)     |        |  limiting)  |
             +-------------+        +-------------+
```

### Data flow

1. **Discovery** scans the Tailscale network for peers running soul-mesh
2. **Election** scores each node by hardware capability and elects the best one as hub
3. **Transport** opens authenticated WebSocket connections between nodes
4. **Sync** routes all writes through the hub; queues writes offline when the hub is unreachable
5. **Linking** pairs new devices to the same account via short-lived codes

### Design principles

- **Dependency injection** -- every module takes its dependencies as constructor parameters. No global singletons.
- **Standalone** -- zero imports from soul-os. Works independently with `pip install`.
- **Security boundaries** -- table/column allowlists for SQL, JWT auth for transport, rate limiting for linking.
- **Offline-first** -- non-hub nodes queue writes in SQLite and replay them when the hub returns.

## Modules

### NodeInfo (`soul_mesh.node`)

Local device identity and hardware capability scoring.

```python
from soul_mesh import NodeInfo

node = NodeInfo(node_name="my-pi", port=8340)
await node.init()  # reads RAM, storage, battery status

print(node.id)                # persistent UUID
print(node.capability_score())  # 0-60 weighted score
```

**Capability scoring:**
- RAM: up to 40 pts (8 GiB = max)
- Storage: up to 20 pts (500 GiB = max)
- Battery penalty: 50% reduction if running on battery

Node IDs persist across restarts in `~/.soul-mesh/node_id`. Pass `node_id_path=":memory:"` for ephemeral IDs.

### PeerDiscovery (`soul_mesh.discovery`)

Discovers mesh peers via Tailscale with HMAC nonce verification.

```python
from soul_mesh.discovery import PeerDiscovery

discovery = PeerDiscovery(
    local_node=node,
    discovery_interval=30,      # seconds between scans
    shared_secret="my-secret",  # HMAC peer verification
)
await discovery.start()

peers = discovery.get_online_peers()
```

Probes each Tailscale peer's `/api/mesh/identity` endpoint, verifies HMAC signatures, rejects peers from different accounts, and computes capability scores locally (never trusts self-reported scores).

### HubElection (`soul_mesh.election`)

Elects the most capable node as hub with hysteresis to prevent flip-flopping.

```python
from soul_mesh import elect_hub, HubElection

# Pure function -- no side effects
nodes = [
    {"id": "aaa", "name": "pi-4", "capability": 35.0, "is_hub": True},
    {"id": "bbb", "name": "desktop", "capability": 55.0, "is_hub": False},
]
winner = elect_hub(nodes)  # "bbb" wins (55 > 35 * 1.2)

# Stateful wrapper with local-node awareness
election = HubElection(local_node=node)
winner = election.run(all_nodes)  # updates node.is_hub
```

The current hub keeps its role unless a challenger exceeds its capability by more than 20% (the hysteresis margin). This prevents rapid role switches when nodes have similar scores.

### MeshDB (`soul_mesh.db`)

Thin async SQLite wrapper used by all modules.

```python
from soul_mesh import MeshDB

db = MeshDB(":memory:")  # or a file path
await db.ensure_tables()

# CRUD
await db.insert("settings", {"key": "theme", "value": "dark"})
row = await db.fetch_one("SELECT value FROM settings WHERE key = ?", ("theme",))
rows = await db.fetch_all("SELECT * FROM mesh_nodes WHERE status = 'online'")

# Transactions
async with db.transaction() as cursor:
    await cursor.execute("INSERT INTO link_attempts ...")
    await cursor.execute("UPDATE settings ...")
    # auto-commits on success, rolls back on exception
```

**Tables:** `mesh_nodes`, `pending_writes`, `link_codes`, `link_attempts`, `settings`

**Security:** `insert()` validates table names against a frozen allowlist. All queries use parameterized `?` placeholders.

### Auth (`soul_mesh.auth`)

JWT tokens for authenticating WebSocket connections between nodes.

```python
from soul_mesh import create_mesh_token, verify_mesh_token

token = create_mesh_token(
    node_id="abc-123",
    account_id="acct-456",
    secret="your-32-byte-secret-key-here!!!!!",
    ttl=3600,  # 1 hour
)

claims = verify_mesh_token(token, secret="your-32-byte-secret-key-here!!!!!")
# {"node_id": "abc-123", "account_id": "acct-456", "type": "mesh", ...}
```

Uses HS256. Raises `jwt.ExpiredSignatureError` or `jwt.InvalidSignatureError` on failure.

### MeshTransport (`soul_mesh.transport`)

Authenticated WebSocket mesh communication with automatic reconnection.

```python
from soul_mesh import MeshTransport

transport = MeshTransport(local_node=node, db=db, secret="your-secret")

# Register message handlers
transport.on("chat", handle_chat_message)
transport.on("mesh_write", handle_write)

await transport.start()  # connects to all known peers

# Send messages
await transport.send(peer_id, "chat", {"text": "hello"})
await transport.send_to_hub("mesh_write", {"table": "events", "data": {...}})
await transport.broadcast("status_update", {"status": "online"})
```

**Features:**
- Exponential backoff reconnection (1s to 5min max)
- 1 MiB message size limit
- 30s WebSocket heartbeat
- JSON message routing with type-based dispatch
- Supports both inbound (server-accepted) and outbound (client-initiated) connections

### MeshSync (`soul_mesh.sync`)

Hub-proxy write coordinator with offline queueing.

```python
from soul_mesh import MeshSync

sync = MeshSync(
    local_node=node,
    transport=transport,
    db=db,
    sync_interval=30,  # seconds between replay attempts
)
await sync.start()

# Write data -- routed automatically
await sync.write("events", {"source": "user", "event_type": "click", "payload": "{}"})
# Hub nodes: writes locally
# Non-hub nodes: forwards to hub, queues if hub is unreachable
```

**Offline queue:**
- SQLite-backed `pending_writes` table
- Max 10,000 pending entries (oldest dropped when full)
- Batch replay of 50 writes per cycle
- Max 10 retries per write before marking as failed

**Security:**
- Table allowlist: `events`, `tasks`, `knowledge`, `chat_history`
- Per-table column allowlists filter unauthorized fields
- Cross-account writes rejected via peer account_id verification

### Linking (`soul_mesh.linking`)

Device pairing through rate-limited link codes.

```python
from soul_mesh import generate_link_code, redeem_link_code, get_or_create_account_id

# On the existing device
account_id = await get_or_create_account_id(db)
code = await generate_link_code(db, account_id)
# Share this 16-character code with the new device

# On the new device
result = await redeem_link_code(db, code, node_id="new-node-id", ip_address="192.168.1.5")
if result:
    print(f"Linked to account: {result}")
```

**Security:**
- Codes expire after 10 minutes
- Per-IP rate limit: 5 attempts per 15 minutes
- Global rate limit: 50 attempts per 15 minutes
- Rate limit checks are transactional (atomic read + insert)
- Expired codes are cleaned up on each generation

## Configuration

Environment variables (all optional):

| Variable | Default | Description |
|----------|---------|-------------|
| `MESH_NODE_NAME` | hostname | Human-readable node name |
| `MESH_PORT` | `8340` | Port for mesh API |
| `MESH_DISCOVERY_INTERVAL` | `30` | Seconds between discovery scans |
| `MESH_SHARED_SECRET` | `""` | Shared secret for HMAC peer verification |

## Testing

```bash
pip install -e ".[dev]"
pytest -v
```

100 tests across 6 test files:

| Test file | Tests | Covers |
|-----------|-------|--------|
| test_election.py | 16 | Hub election, hysteresis, tie-breaking |
| test_db.py | 17 | CRUD, insert allowlist, transactions, table creation |
| test_auth.py | 9 | JWT creation, verification, expiry, invalid tokens |
| test_transport.py | 20 | WebSocket connections, message routing, send/broadcast |
| test_sync.py | 20 | Hub/non-hub writes, offline queue, replay, security checks |
| test_linking.py | 18 | Code generation, redemption, rate limiting, account management |

## License

Apache-2.0
