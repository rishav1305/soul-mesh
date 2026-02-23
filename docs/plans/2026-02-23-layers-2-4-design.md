# Soul-Mesh Layers 2-4 Design

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:writing-plans to create implementation plans for each layer.

**Goal:** Extend soul-mesh from a cluster inventory system to a full homelab compute platform with LLM inference routing, shared storage, and a terminal dashboard.

**Build order:** Layer 4 (TUI Dashboard) -> Layer 2 (LLM Inference) -> Layer 3 (Shared Filesystem)

**Rationale:** Dashboard first (quick win, makes existing cluster visible), then LLM routing (highest value), then storage (enables model distribution).

---

## Layer 4: TUI Dashboard

**Module**: `soul_mesh/dashboard.py`
**CLI**: `soul-mesh dashboard [--hub HOST:PORT]`
**Dependency**: `textual>=0.80` (optional `[dashboard]` extra)

### Screens

1. **Cluster Overview** (default) -- node table with live CPU/RAM/storage sparklines (last 30 data points). Header shows aggregated totals. Stale/offline nodes highlighted in red. Low-RAM warnings (>85% used) in yellow.

2. **Node Detail** -- select a node, see full specs + live graphs (CPU%, RAM%, load average). Heartbeat history. Model list (which LLMs are loaded, RAM consumed per model).

3. **Remote Shell** -- select a node, type commands, see stdout/stderr. Hub relays commands to target node via WebSocket. Simple command-response (not a full TTY).

4. **Model Manager** -- table of all models across all nodes. Actions: load model on node, unload model, see which nodes can fit a model based on available RAM.

5. **Alerts** -- scrolling alert log. Auto-generated events: node went stale, node came online, RAM threshold crossed, heartbeat missed. Filterable by severity.

### Data Sources

- Screens 1-2, 5: `GET /api/mesh/nodes`, `GET /api/mesh/status` (poll every 3s)
- Screen 3: `POST /api/mesh/exec` endpoint (hub relays to node via WebSocket)
- Screen 4: `GET /api/mesh/models`, `POST /api/mesh/models/load`, `POST /api/mesh/models/unload`

### Keybindings

`q` quit, `r` refresh, `enter` drill into node, `escape` back, `l` logs, `1-5` switch screens.

### New Server Endpoints (for dashboard)

- `POST /api/mesh/exec` -- run a command on a target node. Hub relays via WebSocket.
  - Request: `{"node_id": "...", "command": "ls -la"}`
  - Response: `{"stdout": "...", "stderr": "...", "exit_code": 0}`

---

## Layer 2: LLM Inference Distribution

**New modules**:
- `soul_mesh/inference.py` -- proxy router + model registry
- `soul_mesh/executor.py` -- remote command execution (shared with dashboard)

### Heartbeat Extension

Agents add a `models` field to heartbeats listing running llama-server instances:

```json
{
  "node_id": "...",
  "cpu": {...},
  "memory": {...},
  "storage": {...},
  "models": [
    {"name": "qwen2.5-7b-q4", "port": 8080, "vram_mb": 0, "ram_mb": 4500}
  ]
}
```

### Model Registry

Hub stores model state in a `models` DB table, updated from heartbeats.

**Table: `models`**
- `id` TEXT PK (auto-generated)
- `node_id` TEXT FK -> nodes.id
- `name` TEXT (model name, e.g., "qwen2.5-7b-q4")
- `port` INT (llama-server port on that node)
- `ram_mb` INT (RAM consumed by this model)
- `status` TEXT ("running" | "stopped")

### Inference Proxy

`POST /api/mesh/inference` -- OpenAI-compatible chat completion proxy.

Flow:
1. Client sends request with `model` field
2. Hub looks up which nodes have that model running
3. Routes to the node with lowest CPU usage
4. Forwards request to that node's llama-server (HTTP proxy)
5. Streams response back (SSE)

If no node has the model: return 404 with available model list.

### Routing Logic

1. Find nodes where `models.name = requested_model AND models.status = 'running'`
2. If multiple: pick node with lowest `cpu_usage_percent` from latest heartbeat
3. If none: return `{"error": "model not found", "available": [...]}`
4. Future: auto-load model on best-fit node (requires Layer 3 for model files)

### Remote Execution

`POST /api/mesh/exec` -- run a command on a specific node.

Hub sends command to target node via existing WebSocket connection. Node executes, returns result. Used by:
- Dashboard remote shell
- Model lifecycle (start/stop llama-server on nodes)

Protocol: Hub sends `{"type": "exec", "command": "...", "exec_id": "..."}` over WebSocket. Agent runs command, sends back `{"type": "exec_result", "exec_id": "...", "stdout": "...", "stderr": "...", "exit_code": 0}`.

### New Server Endpoints

- `POST /api/mesh/inference` -- OpenAI-compatible proxy (streaming SSE)
- `GET /api/mesh/models` -- list all models across cluster
- `POST /api/mesh/models/load` -- start llama-server with a model on a node
- `POST /api/mesh/models/unload` -- stop llama-server on a node

### CLI Additions

- `soul-mesh models` -- list models across cluster
- `soul-mesh infer --model <name> --prompt "..."` -- quick inference from terminal

---

## Layer 3: Shared Filesystem

**New module**: `soul_mesh/storage.py`

### File Registry

Hub maintains a `files` table tracking shared files.

**Table: `files`**
- `id` TEXT PK
- `path` TEXT (relative path within shared namespace)
- `size_bytes` INT
- `sha256` TEXT
- `owner_node_id` TEXT FK -> nodes.id
- `replicas` TEXT (JSON array of node_ids that have the file)
- `created_at` TEXT
- `updated_at` TEXT

### Operations

**Push**: Node shares a file with the cluster.
1. Node computes sha256, sends metadata to hub via `POST /api/mesh/files/push`
2. Hub records file in registry with node as owner
3. Other nodes can pull the file

**Pull**: Node requests a file.
1. Node calls `POST /api/mesh/files/pull` with file path
2. Hub returns list of nodes that have the file
3. Node rsyncs from the nearest one over SSH

**Auto-sync** (optional): Designate `~/.soul-mesh/shared/` as a sync directory. Background task checks for new/changed files every 30s, auto-pushes to registry.

### Model Distribution Integration

When Layer 2 requests a model load but the node doesn't have the GGUF file:
1. Check `files` table for the model file
2. If another node has it, trigger a pull to the requesting node
3. Then start llama-server

### New Server Endpoints

- `GET /api/mesh/files` -- list all shared files with replica info
- `POST /api/mesh/files/push` -- register a file for sharing
- `POST /api/mesh/files/pull` -- request file transfer to a node

### CLI Additions

- `soul-mesh files` -- list shared files
- `soul-mesh push <path>` -- share a file with the cluster
- `soul-mesh pull <path>` -- download a file from the cluster

### Prerequisites

- Passwordless SSH between nodes (already configured for titan-pi <-> titan-pc)
- rsync installed on all nodes

---

## Summary

| Layer | Module | Lines (est.) | New Endpoints | New DB Tables |
|-------|--------|-------------|---------------|---------------|
| 4 | dashboard.py | ~400 | /exec | - |
| 2 | inference.py, executor.py | ~350 | /inference, /models, /exec | models |
| 3 | storage.py | ~250 | /files, /files/push, /files/pull | files |

Total estimated new code: ~1,000 lines across 4 new modules + server endpoint additions.

Build order: Layer 4 -> Layer 2 -> Layer 3.
