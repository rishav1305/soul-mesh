# Soul-Mesh v1 Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Transform soul-mesh from a data-sync library into a runnable homelab cluster orchestrator where devices form a unified compute mesh with a manually designated hub.

**Architecture:** Hub-agent model over WebSocket. Hub runs a FastAPI control plane accepting agent heartbeats. Agents report live CPU/RAM/disk every 10s. CLI provides init, serve, status, nodes commands. mDNS for LAN discovery, Tailscale optional.

**Tech Stack:** Python 3.11+, aiosqlite, websockets, PyJWT, structlog, click, pyyaml, zeroconf, FastAPI (optional extra)

**Design doc:** `docs/plans/2026-02-22-distributed-compute-mesh-design.md`

**Existing code:** 8 modules, 100 tests. auth.py, transport.py, election.py, linking.py survive unchanged. node.py, discovery.py, db.py get refactored. sync.py gets removed.

---

### Task 1: Update pyproject.toml with new dependencies and CLI entry point

**Files:**
- Modify: `pyproject.toml`

**Step 1: Update pyproject.toml**

Replace the entire `pyproject.toml` with:

```toml
[build-system]
requires = ["setuptools>=75.0"]
build-backend = "setuptools.build_meta"

[project]
name = "soul-mesh"
version = "0.2.0"
description = "Distributed compute mesh for homelabbers -- unified RAM, storage, and CPU across devices."
readme = "README.md"
license = {text = "Apache-2.0"}
requires-python = ">=3.11"
authors = [{name = "Rishav"}]
keywords = ["mesh", "distributed", "cluster", "homelab", "compute"]

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
dev = [
    "pytest>=7.0",
    "pytest-asyncio>=0.21",
    "httpx>=0.27",
    "ruff>=0.8.0",
]

[project.scripts]
soul-mesh = "soul_mesh.cli:main"

[tool.setuptools.packages.find]
where = ["."]
include = ["soul_mesh*"]

[tool.ruff]
target-version = "py311"
line-length = 120

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
```

**Step 2: Install new dependencies**

Run: `cd /home/rishav/soul/soul-mesh && pip install -e ".[dev,server]"`
Expected: SUCCESS, click/pyyaml/zeroconf/fastapi installed

**Step 3: Verify existing tests still pass**

Run: `.venv/bin/python -m pytest tests/ -v --tb=short`
Expected: 100 passed

**Step 4: Commit**

```bash
git add pyproject.toml
git commit -m "chore: update deps for v0.2 -- add click, pyyaml, zeroconf, fastapi"
```

---

### Task 2: Create config.py -- YAML config loader with env var overrides

**Files:**
- Create: `soul_mesh/config.py`
- Create: `tests/test_config.py`

**Step 1: Write the failing tests**

Create `tests/test_config.py` with tests covering:
- `MeshConfig` defaults (role=agent, port=8340, name=hostname, hub="", secret="", mdns=True, tailscale=False, heartbeat_interval=10, stale_timeout=30)
- `MeshConfig.from_dict()` parsing nested YAML structure (`node.role`, `node.hub`, `auth.secret`, `discovery.mdns`)
- `cfg.with_env_overrides()` for MESH_ROLE, MESH_PORT, MESH_HUB, MESH_SECRET (use monkeypatch)
- `load_config(path)` reads YAML file then applies env overrides
- `load_config` with missing file returns defaults

Target: 19 tests

**Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_config.py -v --tb=short`
Expected: FAIL with `ModuleNotFoundError: No module named 'soul_mesh.config'`

**Step 3: Write minimal implementation**

Create `soul_mesh/config.py`:
- `CONFIG_DIR = Path.home() / ".soul-mesh"`
- `DEFAULT_CONFIG_PATH = CONFIG_DIR / "config.yaml"`
- `@dataclass MeshConfig` with fields: name (default hostname), role, port, hub, secret, mdns, tailscale, heartbeat_interval, stale_timeout
- `from_dict(cls, data)` class method parsing nested YAML dict
- `with_env_overrides(self)` returning new config with MESH_* env vars applied
- `load_config(path)` loading YAML then applying env overrides

**Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_config.py -v --tb=short`
Expected: 19 passed

**Step 5: Run full test suite**

Run: `.venv/bin/python -m pytest tests/ -v --tb=short`
Expected: 119 passed (100 existing + 19 new)

**Step 6: Commit**

```bash
git add soul_mesh/config.py tests/test_config.py
git commit -m "feat: add config module -- YAML loader with env var overrides"
```

---

### Task 3: Create resources.py -- live system resource collection

**Files:**
- Create: `soul_mesh/resources.py`
- Create: `tests/test_resources.py`

**Step 1: Write the failing tests**

Create `tests/test_resources.py` with tests covering:
- `get_cpu_info()` returns dict with `cores` (int >= 1), `usage_percent` (float), `load_avg_1m` (float)
- `get_memory_info()` returns dict with `total_mb` (int > 0), `available_mb` (int), `used_percent` (0-100)
- `get_storage_info()` returns dict with `mounts` (list with at least 1 entry), each mount has `path`, `total_gb`, `free_gb`, root mount "/" present
- `get_system_snapshot()` returns combined dict with `cpu`, `memory`, `storage` keys

Target: 14 tests (these run against the real system -- no mocking needed for live resource reads)

**Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_resources.py -v --tb=short`
Expected: FAIL with `ModuleNotFoundError: No module named 'soul_mesh.resources'`

**Step 3: Write minimal implementation**

Create `soul_mesh/resources.py`:
- `get_cpu_info()` -- `os.cpu_count()` for cores, `os.getloadavg()` for load, usage from load/cores ratio
- `get_memory_info()` -- parse `/proc/meminfo` (Linux) or `sysctl` (macOS), compute used_percent
- `get_storage_info()` -- `df -BG --output=target,size,avail` (Linux) or `df -g` (macOS), parse mounts
- `get_system_snapshot()` -- `asyncio.gather` all three above
- No psutil dependency. Uses `/proc/meminfo`, `/proc/stat`, `df`, `sysctl`.

**Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_resources.py -v --tb=short`
Expected: 14 passed

**Step 5: Commit**

```bash
git add soul_mesh/resources.py tests/test_resources.py
git commit -m "feat: add resources module -- live CPU, RAM, disk collection without psutil"
```

---

### Task 4: Refactor db.py -- new schema for cluster management

**Files:**
- Modify: `soul_mesh/db.py`
- Rewrite: `tests/test_db.py`

**Step 1: Write the failing tests**

Rewrite `tests/test_db.py` with tests covering:
- Connection: memory db reuses connection, ensure_tables creates `nodes`, `heartbeats`, `settings`, `link_codes`, `link_attempts`
- CRUD: insert + fetch_one, insert returns rowid, insert rejects unknown table, fetch_all, fetch_one returns None, execute update
- Transaction: commits on success, rolls back on exception
- Node operations: insert node with defaults (role=agent, port=8340, status=offline), `upsert_node()` insert and update, insert heartbeat, get online nodes

Schema changes:
- `mesh_nodes` table renamed to `nodes`
- `pending_writes` table removed
- `nodes` table gets: `role`, `cpu_cores`, `ram_total_mb`, `storage_total_gb`, `account_id`
- New `heartbeats` table for time-series resource snapshots
- New `upsert_node(data)` method using INSERT ... ON CONFLICT DO UPDATE
- `_INSERTABLE_TABLES` updated to: `nodes`, `heartbeats`, `settings`, `link_codes`, `link_attempts`

Target: 18 tests

**Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_db.py -v --tb=short`
Expected: FAIL (new tables/methods don't exist)

**Step 3: Write the implementation**

Rewrite `soul_mesh/db.py`:
- Update `_INSERTABLE_TABLES` frozenset: `nodes`, `heartbeats`, `settings`, `link_codes`, `link_attempts`
- Keep: `MeshDB.__init__`, `_connect`, `_close`, `fetch_all`, `fetch_one`, `execute`, `insert`, `transaction`
- Add: `upsert_node(data)` -- INSERT ... ON CONFLICT(id) DO UPDATE for name, host, port, platform, arch, cpu_cores, ram_total_mb, storage_total_gb, last_heartbeat
- Rewrite `ensure_tables()`: create `nodes` (with account_id, cpu_cores, ram_total_mb, storage_total_gb, role, status, last_heartbeat, joined_at), `heartbeats`, `link_codes`, `link_attempts`, `settings`

**Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_db.py -v --tb=short`
Expected: 18 passed

**Step 5: Verify auth + election tests still pass** (they don't use db)

Run: `.venv/bin/python -m pytest tests/test_db.py tests/test_auth.py tests/test_election.py -v --tb=short`
Expected: 43 passed (18 + 9 + 16)

**Step 6: Commit**

```bash
git add soul_mesh/db.py tests/test_db.py
git commit -m "refactor: rewrite db.py for cluster schema -- nodes, heartbeats, upsert_node"
```

---

### Task 5: Create hub.py -- hub control plane (node registry + resource aggregation)

**Files:**
- Create: `soul_mesh/hub.py`
- Create: `tests/test_hub.py`

**Step 1: Write the failing tests**

Create `tests/test_hub.py` with tests covering:
- `hub.register_node(data)` -- registers node in db, sets status online, records cpu_cores, records initial heartbeat
- `hub.register_node()` twice with same id -- updates name (upsert behavior)
- `hub.process_heartbeat(node_id, snapshot)` -- updates status to online, records heartbeat row with cpu/ram/storage
- `hub.mark_stale_nodes(timeout_seconds=30)` -- returns stale ids, sets status to "stale", fresh nodes not marked
- `hub.cluster_totals()` -- sums cpu_cores, ram_total_mb across online nodes, excludes stale, returns nodes_online count
- `hub.list_nodes()` -- returns all registered nodes

Uses `MeshDB(":memory:")` fixture with `ensure_tables()`.

Target: 14 tests

**Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_hub.py -v --tb=short`
Expected: FAIL with `ModuleNotFoundError: No module named 'soul_mesh.hub'`

**Step 3: Write minimal implementation**

Create `soul_mesh/hub.py`:
- `Hub(db: MeshDB)` class
- `register_node(data)` -- calls `db.upsert_node()`, sets status online, inserts heartbeat row
- `process_heartbeat(node_id, snapshot)` -- updates last_heartbeat + status, inserts heartbeat row
- `mark_stale_nodes(timeout_seconds)` -- SELECT nodes where last_heartbeat older than timeout, UPDATE to stale, return list of ids
- `cluster_totals()` -- SELECT SUM(cpu_cores), SUM(ram_total_mb), COUNT(*) FROM nodes WHERE status = 'online'
- `list_nodes()` -- SELECT * FROM nodes ORDER BY name

**Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_hub.py -v --tb=short`
Expected: 14 passed

**Step 5: Commit**

```bash
git add soul_mesh/hub.py tests/test_hub.py
git commit -m "feat: add hub control plane -- node registry, heartbeats, resource aggregation"
```

---

### Task 6: Create agent.py -- agent heartbeat loop

**Files:**
- Create: `soul_mesh/agent.py`
- Create: `tests/test_agent.py`

**Step 1: Write the failing tests**

Create `tests/test_agent.py` with tests covering:
- `Agent(config)` stores config, not running initially
- `agent._build_heartbeat()` returns dict with: node_id (non-empty string), cpu (with cores), memory (with total_mb), storage (with mounts), name, platform, arch
- `agent.stop()` sets running to False

Uses `MeshConfig(name="test", role="agent", hub="10.0.0.1:8340", secret="s")`.
Agent uses `node_id_path=":memory:"` for tests (no filesystem).
Does NOT test actual WebSocket connection (that's integration).

Target: 9 tests

**Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_agent.py -v --tb=short`
Expected: FAIL with `ModuleNotFoundError: No module named 'soul_mesh.agent'`

**Step 3: Write minimal implementation**

Create `soul_mesh/agent.py`:
- `Agent(config: MeshConfig)` -- stores config, creates NodeInfo with `:memory:` id path
- `_init_node()` -- calls `node.init()`
- `_build_heartbeat()` -- calls `get_system_snapshot()`, merges with node identity (node_id, name, host, platform, arch)
- `start()` -- sets running, inits node, creates heartbeat_loop task
- `stop()` -- cancels task, closes ws
- `_heartbeat_loop()` -- connects to hub ws, sends JSON heartbeat every interval, exponential backoff on failure
- `_ensure_connection()` -- lazy WebSocket connect with JWT token

**Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_agent.py -v --tb=short`
Expected: 9 passed

**Step 5: Commit**

```bash
git add soul_mesh/agent.py tests/test_agent.py
git commit -m "feat: add agent module -- heartbeat loop with resource snapshots"
```

---

### Task 7: Create server.py -- FastAPI endpoints for hub

**Files:**
- Create: `soul_mesh/server.py`
- Create: `tests/test_server.py`

**Step 1: Write the failing tests**

Create `tests/test_server.py` with tests covering:
- `GET /api/mesh/health` returns 200 with `{"status": "ok"}`
- `GET /api/mesh/status` empty cluster returns nodes_online=0, cpu_cores=0
- `GET /api/mesh/status` with registered nodes returns correct totals
- `GET /api/mesh/nodes` empty returns `[]`
- `GET /api/mesh/nodes` with registered nodes returns list with name, cpu_cores, ram_total_mb
- `GET /api/mesh/identity` returns node_id, name, port

Uses `httpx.AsyncClient` with `ASGITransport(app=app)`. Fixture creates `MeshDB(":memory:")` and `create_app(db)`.

Target: 7 tests

**Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_server.py -v --tb=short`
Expected: FAIL with `ModuleNotFoundError: No module named 'soul_mesh.server'`

**Step 3: Write minimal implementation**

Create `soul_mesh/server.py`:
- `create_app(db: MeshDB, node: NodeInfo | None = None) -> FastAPI`
- Routes:
  - `GET /api/mesh/health` -- returns `{"status": "ok"}`
  - `GET /api/mesh/status` -- calls `hub.cluster_totals()`
  - `GET /api/mesh/nodes` -- calls `hub.list_nodes()`
  - `GET /api/mesh/identity` -- returns node info dict
- Stores db, hub, node on `app.state`

**Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_server.py -v --tb=short`
Expected: 7 passed

**Step 5: Commit**

```bash
git add soul_mesh/server.py tests/test_server.py
git commit -m "feat: add FastAPI server -- health, status, nodes, identity endpoints"
```

---

### Task 8: Create cli.py -- Click-based CLI (init, serve, status, nodes)

**Files:**
- Create: `soul_mesh/cli.py`
- Create: `tests/test_cli.py`

**Step 1: Write the failing tests**

Create `tests/test_cli.py` using `click.testing.CliRunner` with tests covering:
- `soul-mesh init --role hub` creates config.yaml with role=hub, generates secret (len >= 32), creates node_id file
- `soul-mesh init --role agent --hub 192.168.1.10:8340 --secret <key>` creates config with hub address and secret
- `soul-mesh init --role agent` without --hub fails with error
- `soul-mesh init --role hub --name my-pi` sets node name in config
- `soul-mesh status` with no nodes shows "0" in output
- `soul-mesh nodes` with no nodes shows "No nodes" message

All commands use `--config-dir tmp_path` to avoid touching real `~/.soul-mesh/`.

Target: 7 tests

**Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_cli.py -v --tb=short`
Expected: FAIL with `ModuleNotFoundError: No module named 'soul_mesh.cli'`

**Step 3: Write minimal implementation**

Create `soul_mesh/cli.py`:
- `@click.group() def main()` -- top-level group
- `@main.command() def init(role, hub, secret, name, port, config_dir)` -- generates config.yaml and node_id
- `@main.command() def serve(config_dir)` -- starts hub (uvicorn) or agent based on role
- `@main.command() def status(config_dir)` -- reads db, shows cluster totals
- `@main.command() def nodes(config_dir)` -- reads db, lists nodes table

**Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_cli.py -v --tb=short`
Expected: 7 passed

**Step 5: Commit**

```bash
git add soul_mesh/cli.py tests/test_cli.py
git commit -m "feat: add CLI -- init, serve, status, nodes commands"
```

---

### Task 9: Update __init__.py, remove sync.py, fix linking.py for new schema

**Files:**
- Modify: `soul_mesh/__init__.py`
- Delete: `soul_mesh/sync.py`
- Delete: `tests/test_sync.py`
- Modify: `soul_mesh/linking.py` -- change `mesh_nodes` references to `nodes`
- Modify: `tests/test_linking.py` -- update for new schema

**Step 1: Update __init__.py**

Replace with exports for new modules (Hub, Agent, MeshConfig, load_config). Remove MeshSync import. Bump version to 0.2.0.

**Step 2: Remove sync module**

Delete `soul_mesh/sync.py` and `tests/test_sync.py`.

**Step 3: Fix linking.py**

In `soul_mesh/linking.py`:
- Replace `"UPDATE mesh_nodes SET account_id = ? WHERE id = ?"` with `"UPDATE nodes SET account_id = ? WHERE id = ?"`

**Step 4: Add account_id column to nodes table**

In `soul_mesh/db.py` `ensure_tables()`, add `account_id TEXT DEFAULT ''` to the `nodes` CREATE TABLE after `name`.

In `upsert_node()`, add `account_id` to the INSERT columns and ON CONFLICT UPDATE.

**Step 5: Fix test_linking.py**

Update the fixture to use `db.ensure_tables()` (which now creates `nodes` instead of `mesh_nodes`). Update any SQL in tests that references `mesh_nodes` to `nodes`.

**Step 6: Run full test suite**

Run: `.venv/bin/python -m pytest tests/ -v --tb=short`
Expected: All tests pass (sync tests removed, linking tests updated)

**Step 7: Commit**

```bash
git add -A
git commit -m "refactor: remove sync module, update linking and init for new cluster schema"
```

---

### Task 10: Final verification and push

**Files:**
- No new files

**Step 1: Run full test suite**

Run: `.venv/bin/python -m pytest tests/ -v --tb=short`
Expected: All tests pass. Target count: ~95 tests (100 original - 20 sync + ~15 new modules)

**Step 2: Verify no brain imports**

Run: `grep -r "from brain" soul_mesh/`
Expected: No output (zero matches)

**Step 3: Verify pip install works**

Run: `pip install -e . && python -c "from soul_mesh import Hub, Agent, MeshConfig; print('OK')"`
Expected: `OK`

**Step 4: Verify CLI entry point works**

Run: `soul-mesh --help`
Expected: Shows help with init, serve, status, nodes commands

**Step 5: Push to Gitea**

```bash
git push origin master
```
