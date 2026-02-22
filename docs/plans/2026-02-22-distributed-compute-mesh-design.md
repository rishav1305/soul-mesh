# Soul-Mesh: Distributed Compute Mesh for Homelabbers

**Date:** 2026-02-22
**Status:** Approved
**Approach:** Layered Framework (A)

## Vision

Soul-mesh is a homelab cluster orchestrator. Install it on every device in your network -- Pi, laptop, desktop, server, phone -- and they form a managed cluster. One device is the hub (manually selected). Users interact with the hub only. Together the devices present unified RAM, unified storage, and unified CPU.

### User Experience

```bash
# First device (the hub):
$ soul-mesh init --role hub
  Generated node ID: a1b2c3d4
  Generated cluster secret: xK9m...
  Config written to ~/.soul-mesh/config.yaml

  To add devices, run on each one:
    soul-mesh init --hub 192.168.1.10 --secret xK9m...

# Every other device:
$ soul-mesh init --hub 192.168.1.10 --secret xK9m...
$ soul-mesh serve

# On the hub:
$ soul-mesh status
  soul-mesh cluster
    Hub: titan-desktop (192.168.1.10)
    Nodes: 4 online

    NODE            CPU     RAM        STORAGE     STATUS
    titan-desktop   12c     28.5/32GB  841GB free  hub
    rasp-pi-4       4c      3.2/4GB    24GB free   online
    macbook         8c      12.1/16GB  380GB free  online
    phone-pixel     8c      4.2/6GB    64GB free   online

    CLUSTER TOTAL   32c     48/58GB    1.3TB free
```

## Layer Architecture

Each layer is independently useful. Later layers build on earlier ones.

```
Layer 0: Mesh Core       (refactor existing)
         - Node identity + capability reporting
         - Manual hub designation
         - Agent heartbeat to hub
         - WebSocket transport + JWT auth
         - mDNS discovery (LAN) + Tailscale (optional)

Layer 1: Mesh Server     (new)
         - "soul-mesh serve --role hub" / "soul-mesh serve --hub <ip>"
         - Hub accepts agent connections
         - Agents report resources every 10 seconds
         - CLI: soul-mesh init, serve, status, nodes

Layer 2: Task Engine     (future)
         - Submit tasks to hub: "soul-mesh run 'python train.py'"
         - Hub picks best node based on requirements (CPU, RAM, GPU)
         - Stream stdout/stderr back to hub
         - Task status tracking, retries

Layer 3: Storage Index   (future)
         - Agents report mount points and sizes
         - Hub maintains index of where data lives
         - "soul-mesh ls" shows files across all nodes
         - "soul-mesh fetch <node>:<path>" pulls a file to local

Layer 4: Dashboard       (future)
         - Web UI on the hub showing cluster overview
         - Live resource utilization per node
         - Task history, storage map
```

**v1 ships Layers 0 + 1.** pip extras for future layers:
- `pip install soul-mesh` -- core library
- `pip install soul-mesh[server]` -- runnable service (FastAPI + uvicorn)
- `pip install soul-mesh[tasks]` -- task distribution (Layer 2)
- `pip install soul-mesh[storage]` -- shared storage (Layer 3)

## Component Design

### Node Agent

Every device runs an agent. The agent does three things:
1. Reports resources to the hub every 10 seconds (heartbeat)
2. Accepts task execution requests from the hub (Layer 2)
3. Responds to storage queries from the hub (Layer 3)

```
Agent lifecycle:
  init -> load config -> connect to hub -> heartbeat loop
                                              |
                              +---------------+---------------+
                              |               |               |
                        report resources  accept tasks   serve files
```

Heartbeat payload:

```json
{
    "node_id": "abc-123",
    "timestamp": "2026-02-22T14:30:00Z",
    "cpu": {
        "cores": 4,
        "usage_percent": 23.5,
        "load_avg_1m": 1.2
    },
    "memory": {
        "total_mb": 4096,
        "available_mb": 2800,
        "used_percent": 31.6
    },
    "storage": {
        "mounts": [
            {"path": "/", "total_gb": 32, "free_gb": 24},
            {"path": "/mnt/data", "total_gb": 500, "free_gb": 340}
        ]
    },
    "status": "online"
}
```

No psutil dependency. Uses `/proc/meminfo`, `/proc/stat`, `df` (Linux), `sysctl` (macOS).

### Hub

The hub is a regular agent that also runs the control plane:

| Component | Responsibility |
|-----------|---------------|
| Node Registry | Track all agents, their resources, last heartbeat |
| Resource Aggregator | Sum totals, detect stale nodes (no heartbeat > 30s) |
| Task Scheduler | Accept tasks, pick best node, dispatch, track results (Layer 2) |
| Storage Index | Know what files/mounts exist on which node (Layer 3) |
| API Server | FastAPI endpoints for CLI and dashboard |
| WebSocket Hub | Accept agent connections, route messages |

Hub stores cluster state in its local SQLite. If the hub goes down, agents keep running but can't accept new tasks or report status. When hub returns, agents reconnect automatically.

### Transport Protocol

WebSocket with JSON messages. Type-based dispatch (existing transport.py).

Hub-to-Agent messages:

| Type | Purpose |
|------|---------|
| `task.run` | Execute a command on this node (Layer 2) |
| `task.cancel` | Cancel a running task (Layer 2) |
| `storage.query` | List files at a path (Layer 3) |
| `storage.fetch` | Stream a file to the hub (Layer 3) |
| `ping` | Heartbeat check |

Agent-to-Hub messages:

| Type | Purpose |
|------|---------|
| `heartbeat` | Resource snapshot (every 10s) |
| `task.status` | Task progress update (Layer 2) |
| `task.output` | Stdout/stderr stream chunk (Layer 2) |
| `storage.report` | Mount points and sizes (Layer 3) |

### Discovery

Pluggable backends, tried in order:

1. **Config file** (explicit hub IP) -- always works, required for agents
2. **mDNS/Zeroconf** (`_soul-mesh._tcp.local.`) -- LAN auto-discovery, hub announces itself
3. **Tailscale** (`tailscale status --json`) -- optional, for cross-NAT setups

For v1, agents must know the hub address via `soul-mesh init --hub <ip>` or config. mDNS is additive -- the hub announces itself so agents can find it on the same LAN without explicit config.

## Configuration

### Config File

Generated by `soul-mesh init`, stored at `~/.soul-mesh/config.yaml`:

```yaml
# Hub node
node:
  name: titan-desktop
  role: hub
  port: 8340

auth:
  secret: "auto-generated-32-byte-key"

discovery:
  mdns: true
  tailscale: false
```

```yaml
# Agent node
node:
  name: rasp-pi-4
  role: agent
  port: 8340
  hub: 192.168.1.10:8340

auth:
  secret: "same-key-as-hub"
```

### Environment Variable Overrides

Every config field can be overridden with `MESH_` prefix:

| Env Var | Overrides |
|---------|-----------|
| `MESH_NODE_NAME` | `node.name` |
| `MESH_ROLE` | `node.role` |
| `MESH_PORT` | `node.port` |
| `MESH_HUB` | `node.hub` |
| `MESH_SECRET` | `auth.secret` |
| `MESH_MDNS` | `discovery.mdns` |

## Error Handling & Resilience

| Scenario | Behavior |
|----------|----------|
| Agent loses connection to hub | Exponential backoff reconnect (1s to 5min). Agent keeps running locally. |
| Hub goes down | Agents retry connections. No new tasks accepted. Running tasks continue on their nodes. |
| Agent goes stale (no heartbeat > 30s) | Hub marks it `offline`. Tasks on that node marked `lost`. |
| Agent comes back online | Reconnects, sends heartbeat, hub marks it `online`. |
| Hub changes (user designates new hub) | Old hub demotes to agent. New hub starts accepting connections. Agents re-init to point at new hub. |
| Network partition | Agents on hub's side keep working. Isolated agents retry. No split-brain -- only one hub, manually chosen. |

No split-brain by design. Manual hub selection means exactly one hub at all times. No consensus algorithm needed. Trade-off: if hub dies, manual intervention required to promote a new one.

## Changes to Existing Code

### Files that survive unchanged (63 tests):

| File | Tests | Notes |
|------|-------|-------|
| `auth.py` | 9 | JWT create/verify -- untouched |
| `transport.py` | 20 | WebSocket transport -- untouched, add new message types via handlers |
| `election.py` | 16 | Keep scoring logic for display. Remove auto-elect. |
| `linking.py` | 18 | Link codes for sharing cluster secret with new devices |

### Files that get refactored (37 tests replaced):

| File | Change |
|------|--------|
| `node.py` | Extend with live resource reporting (CPU usage, memory available, disk free) |
| `discovery.py` | Extract Tailscale into backend class. Add mDNS backend (zeroconf). Add static backend. |
| `db.py` | New schema: `nodes`, `heartbeats`, `tasks`, `storage_index`. Remove soul-os table allowlist. |
| `sync.py` | Remove entirely. Replaced by heartbeat/resource reporting in agent.py. |

### New files:

| File | Purpose |
|------|---------|
| `config.py` | YAML config loader with env var overrides (pydantic-settings) |
| `resources.py` | Live system resource collection (CPU, RAM, disk) without psutil |
| `agent.py` | Agent loop: connect to hub, heartbeat, accept tasks |
| `hub.py` | Hub control plane: node registry, resource aggregation, task routing |
| `server.py` | FastAPI app with mesh API endpoints |
| `cli.py` | Click-based CLI (init, serve, status, nodes) |

## Database Schema

Soul-mesh SQLite stores cluster metadata only. Not user data.

```sql
CREATE TABLE nodes (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    host TEXT NOT NULL,
    port INTEGER DEFAULT 8340,
    role TEXT DEFAULT 'agent',        -- 'hub' or 'agent'
    platform TEXT DEFAULT '',
    arch TEXT DEFAULT '',
    cpu_cores INTEGER DEFAULT 0,
    ram_total_mb INTEGER DEFAULT 0,
    storage_total_gb REAL DEFAULT 0,
    status TEXT DEFAULT 'offline',    -- 'online', 'offline', 'stale'
    last_heartbeat TEXT DEFAULT '',
    joined_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE TABLE heartbeats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    node_id TEXT NOT NULL REFERENCES nodes(id),
    cpu_usage_percent REAL DEFAULT 0,
    cpu_load_1m REAL DEFAULT 0,
    ram_available_mb INTEGER DEFAULT 0,
    ram_used_percent REAL DEFAULT 0,
    storage_free_gb REAL DEFAULT 0,
    recorded_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

-- Layer 2 (future)
CREATE TABLE tasks (
    id TEXT PRIMARY KEY,
    command TEXT NOT NULL,
    node_id TEXT REFERENCES nodes(id),
    status TEXT DEFAULT 'pending',   -- pending, running, completed, failed, lost
    exit_code INTEGER,
    submitted_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    started_at TEXT,
    completed_at TEXT
);

-- Layer 3 (future)
CREATE TABLE storage_index (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    node_id TEXT NOT NULL REFERENCES nodes(id),
    mount_path TEXT NOT NULL,
    total_gb REAL DEFAULT 0,
    free_gb REAL DEFAULT 0,
    updated_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

-- Existing tables (kept)
CREATE TABLE link_codes (
    code TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

CREATE TABLE link_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ip_address TEXT NOT NULL,
    code TEXT NOT NULL,
    success INTEGER DEFAULT 0,
    attempted_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE TABLE settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
```

## Dependencies

```toml
dependencies = [
    "aiosqlite>=0.20.0",
    "structlog>=24.0.0",
    "websockets>=12.0",
    "PyJWT>=2.8.0",
    "click>=8.0",
    "pyyaml>=6.0",
    "zeroconf>=0.131.0",
]

[project.optional-dependencies]
server = ["fastapi>=0.115", "uvicorn>=0.30"]
dev = ["pytest>=7.0", "pytest-asyncio>=0.21", "httpx>=0.27", "ruff>=0.8"]

[project.scripts]
soul-mesh = "soul_mesh.cli:main"
```

## Testing Strategy

| Area | Approach | Est. Tests |
|------|----------|-----------|
| Resource collection | Mock /proc/* and subprocess calls | ~15 |
| Config loading | YAML + env var merging | ~10 |
| Hub node registry | In-memory, add/remove/stale detection | ~15 |
| Agent heartbeat | Mock transport, verify message format | ~10 |
| mDNS discovery | Mock zeroconf, verify service announce/browse | ~12 |
| CLI | Click test runner, verify output format | ~10 |
| API endpoints | httpx TestClient against FastAPI | ~15 |
| Integration | Hub + 2 agents in-memory, end-to-end heartbeat cycle | ~8 |
| Surviving tests | auth, transport, election, linking | 63 |

Target: ~160 tests total for v1.

## v1 Scope

Layers 0 + 1 only. Ship a working mesh agent with:

1. `soul-mesh init` -- generate identity, config, cluster secret
2. `soul-mesh serve` -- run agent (hub or regular)
3. `soul-mesh status` -- cluster overview with unified resource totals
4. `soul-mesh nodes` -- list all nodes with live resource stats
5. `soul-mesh link` -- pair new devices via link code
6. mDNS auto-discovery on LAN
7. WebSocket transport with JWT auth
8. Hub tracks all agents via heartbeat
9. Stale node detection (offline after 30s)

Layers 2-4 (tasks, storage, dashboard) are explicitly out of scope for v1.
