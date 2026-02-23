# Layer 4: TUI Dashboard Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build a Textual-based terminal dashboard that shows live cluster status, node details, alerts, and a remote shell for soul-mesh.

**Architecture:** A Textual App with 4 screens (Cluster Overview, Node Detail, Alerts, Remote Shell). Polls hub REST API every 3s for live data. Remote shell uses a new `/api/mesh/run` endpoint that relays commands to agents via WebSocket. New `executor.py` module handles command relay on both hub and agent sides.

**Tech Stack:** textual>=0.80, httpx (async HTTP client for API polling)

---

### Task 1: Add textual and httpx dependencies

**Files:**
- Modify: `pyproject.toml`

**Step 1: Write the failing test**

```bash
python -c "from textual.app import App; print('ok')"
```

Expected: `ModuleNotFoundError` (textual not installed yet)

**Step 2: Add dependencies**

Add a `dashboard` optional extra to `pyproject.toml`:

```toml
[project.optional-dependencies]
server = ["fastapi>=0.115", "uvicorn>=0.30"]
dashboard = ["textual>=0.80", "httpx>=0.27"]
dev = [
    "pytest>=7.0",
    "pytest-asyncio>=0.21",
    "httpx>=0.27",
    "ruff>=0.8.0",
]
```

**Step 3: Install and verify**

```bash
pip install -e ".[dashboard,dev,server]"
python -c "from textual.app import App; print('ok')"
```

Expected: `ok`

**Step 4: Commit**

```bash
git add pyproject.toml
git commit -m "feat(dashboard): add textual and httpx as dashboard extras"
```

---

### Task 2: Add heartbeat history endpoint to hub and server

The dashboard needs recent heartbeat data for sparklines. Add a method to Hub and an endpoint to the server.

**Files:**
- Modify: `soul_mesh/hub.py` (add `heartbeat_history` method)
- Modify: `soul_mesh/server.py` (add `GET /api/mesh/nodes/{node_id}/heartbeats` endpoint)
- Create: `tests/test_heartbeat_history.py`

**Step 1: Write the failing test**

```python
# tests/test_heartbeat_history.py
import pytest
from soul_mesh.db import MeshDB
from soul_mesh.hub import Hub


@pytest.fixture
async def hub():
    db = MeshDB(":memory:")
    await db.ensure_tables()
    h = Hub(db)
    # Register a node
    await h.register_node({
        "node_id": "node-1",
        "name": "test-node",
        "host": "10.0.0.1",
        "port": 8340,
        "platform": "linux",
        "arch": "x86_64",
        "cpu": {"cores": 4, "usage_percent": 25.0, "load_avg_1m": 1.0},
        "memory": {"total_mb": 8192, "available_mb": 4096, "used_percent": 50.0},
        "storage": {"mounts": [{"path": "/", "total_gb": 500, "free_gb": 200}]},
    })
    return h


async def test_heartbeat_history_returns_recent(hub):
    # Send 5 heartbeats
    for i in range(5):
        await hub.process_heartbeat("node-1", {
            "cpu": {"usage_percent": 10.0 + i, "load_avg_1m": 0.5},
            "memory": {"available_mb": 4000, "used_percent": 50.0},
            "storage": {"mounts": [{"path": "/", "total_gb": 500, "free_gb": 200}]},
        })
    history = await hub.heartbeat_history("node-1", limit=3)
    assert len(history) == 3
    # Most recent first
    assert history[0]["cpu_usage_percent"] >= history[-1]["cpu_usage_percent"]


async def test_heartbeat_history_empty_node(hub):
    history = await hub.heartbeat_history("nonexistent", limit=10)
    assert history == []


async def test_heartbeat_history_default_limit(hub):
    history = await hub.heartbeat_history("node-1", limit=30)
    # Registration creates 1 heartbeat row
    assert len(history) >= 1
    assert len(history) <= 30
```

**Step 2: Run test to verify it fails**

```bash
pytest tests/test_heartbeat_history.py -v
```

Expected: FAIL with `AttributeError: 'Hub' object has no attribute 'heartbeat_history'`

**Step 3: Implement heartbeat_history in Hub**

Add to `soul_mesh/hub.py`:

```python
async def heartbeat_history(self, node_id: str, limit: int = 30) -> list[dict]:
    """Return recent heartbeats for a node, most recent first.

    Parameters
    ----------
    node_id : str
        The node to query.
    limit : int
        Max rows to return (default 30 for sparklines).

    Returns
    -------
    list[dict]
        Heartbeat rows ordered by recorded_at DESC.
    """
    return await self._db.fetch_all(
        "SELECT * FROM heartbeats WHERE node_id = ? ORDER BY recorded_at DESC LIMIT ?",
        (node_id, limit),
    )
```

**Step 4: Add server endpoint**

Add to `soul_mesh/server.py` inside `create_app`, after the `/api/mesh/nodes` endpoint:

```python
@app.get("/api/mesh/nodes/{node_id}/heartbeats")
async def node_heartbeats(node_id: str, limit: int = 30):
    return await app.state.hub.heartbeat_history(node_id, limit=min(limit, 100))
```

**Step 5: Run tests**

```bash
pytest tests/test_heartbeat_history.py -v
```

Expected: 3 PASS

**Step 6: Run full test suite**

```bash
pytest -v
```

Expected: All 203+ tests pass (no regressions)

**Step 7: Commit**

```bash
git add soul_mesh/hub.py soul_mesh/server.py tests/test_heartbeat_history.py
git commit -m "feat(dashboard): add heartbeat history endpoint for sparklines"
```

---

### Task 3: Add remote command relay (executor module + endpoint + agent handler)

The dashboard's Remote Shell screen needs to run commands on remote nodes. This task creates:
1. `soul_mesh/executor.py` -- hub-side command relay logic
2. A `POST /api/mesh/run` server endpoint
3. Agent-side WebSocket handler for command messages

The hub maintains a dict of connected WebSocket objects keyed by node_id. When a command request arrives via REST, the hub sends it over the agent's WebSocket and awaits the response.

**Files:**
- Create: `soul_mesh/executor.py`
- Modify: `soul_mesh/server.py` (add `/api/mesh/run` endpoint, store WebSocket references)
- Modify: `soul_mesh/agent.py` (handle incoming command messages)
- Create: `tests/test_executor.py`

**Step 1: Write the failing test**

```python
# tests/test_executor.py
import asyncio
import pytest
from soul_mesh.executor import CommandRelay


async def test_relay_stores_and_retrieves_connection():
    relay = CommandRelay()
    # Simulate a WebSocket-like object
    class FakeWS:
        def __init__(self):
            self.sent = []
        async def send_json(self, data):
            self.sent.append(data)
    ws = FakeWS()
    relay.register("node-1", ws)
    assert relay.get("node-1") is ws
    relay.unregister("node-1")
    assert relay.get("node-1") is None


async def test_relay_send_command_to_connected_node():
    relay = CommandRelay()
    responses = {}

    class FakeWS:
        def __init__(self):
            self.sent = []
        async def send_json(self, data):
            self.sent.append(data)
            # Simulate agent responding
            cmd_id = data.get("cmd_id")
            if cmd_id:
                relay.deliver_result(cmd_id, {
                    "cmd_id": cmd_id,
                    "stdout": "hello\n",
                    "stderr": "",
                    "exit_code": 0,
                })

    ws = FakeWS()
    relay.register("node-1", ws)

    result = await asyncio.wait_for(
        relay.send_command("node-1", "echo hello"),
        timeout=2.0,
    )
    assert result["stdout"] == "hello\n"
    assert result["exit_code"] == 0


async def test_relay_command_to_unknown_node():
    relay = CommandRelay()
    with pytest.raises(ValueError, match="not connected"):
        await relay.send_command("ghost-node", "ls")
```

**Step 2: Run test to verify it fails**

```bash
pytest tests/test_executor.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'soul_mesh.executor'`

**Step 3: Implement executor.py**

```python
# soul_mesh/executor.py
"""Remote command relay for soul-mesh.

The CommandRelay manages WebSocket references for connected agents
and provides a request-response mechanism for running commands on
remote nodes. The hub stores WebSocket objects when agents connect,
and forwards command requests over the socket when the REST API
receives a run request.
"""

from __future__ import annotations

import asyncio
import uuid

import structlog

logger = structlog.get_logger("soul-mesh.executor")


class CommandRelay:
    """Routes commands from REST API to agents via their WebSocket connections."""

    def __init__(self) -> None:
        self._connections: dict[str, object] = {}
        self._pending: dict[str, asyncio.Future] = {}

    def register(self, node_id: str, ws) -> None:
        """Store a WebSocket reference for a connected agent."""
        self._connections[node_id] = ws
        logger.debug("executor_registered", node_id=node_id)

    def unregister(self, node_id: str) -> None:
        """Remove a WebSocket reference when an agent disconnects."""
        self._connections.pop(node_id, None)
        logger.debug("executor_unregistered", node_id=node_id)

    def get(self, node_id: str):
        """Get the WebSocket for a node, or None if not connected."""
        return self._connections.get(node_id)

    async def send_command(self, node_id: str, command: str, timeout: float = 30.0) -> dict:
        """Send a command to a remote node and wait for the result.

        Parameters
        ----------
        node_id : str
            Target node.
        command : str
            Shell command to run on the node.
        timeout : float
            Seconds to wait for a response (default 30).

        Returns
        -------
        dict
            Keys: ``cmd_id``, ``stdout``, ``stderr``, ``exit_code``.

        Raises
        ------
        ValueError
            If the node is not connected.
        asyncio.TimeoutError
            If the node doesn't respond in time.
        """
        ws = self._connections.get(node_id)
        if ws is None:
            raise ValueError(f"Node {node_id!r} not connected")

        cmd_id = str(uuid.uuid4())
        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[cmd_id] = future

        try:
            await ws.send_json({
                "type": "run_command",
                "cmd_id": cmd_id,
                "command": command,
            })
            return await asyncio.wait_for(future, timeout=timeout)
        finally:
            self._pending.pop(cmd_id, None)

    def deliver_result(self, cmd_id: str, result: dict) -> None:
        """Deliver a command result from an agent (called when agent sends response).

        Parameters
        ----------
        cmd_id : str
            The command ID that was sent to the agent.
        result : dict
            The result payload from the agent.
        """
        future = self._pending.get(cmd_id)
        if future and not future.done():
            future.set_result(result)
```

**Step 4: Update server.py to use CommandRelay**

In `create_app`, after `app.state.secret = secret`:

```python
from soul_mesh.executor import CommandRelay
app.state.relay = CommandRelay()
```

In the `websocket_heartbeat` function, after `await websocket.accept()`:
- After `logger.info("ws_connected", ...)`, add: `app.state.relay.register(node_id, websocket)`
- In the `except WebSocketDisconnect` block, add: `app.state.relay.unregister(node_id)`

Add the REST endpoint after existing endpoints:

```python
from fastapi import HTTPException
from pydantic import BaseModel

class RunCommandRequest(BaseModel):
    node_id: str
    command: str

@app.post("/api/mesh/run")
async def run_command(req: RunCommandRequest):
    try:
        result = await app.state.relay.send_command(req.node_id, req.command)
        return result
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="Command timed out")
```

Also modify the WebSocket handler's message processing loop. After processing heartbeats, check for command result messages:

```python
if data.get("type") == "command_result":
    app.state.relay.deliver_result(data["cmd_id"], data)
    await websocket.send_json({"status": "ok"})
    continue
```

**Step 5: Update agent.py to handle incoming commands**

The agent's WebSocket currently only sends heartbeats. It needs to also listen for incoming command messages from the hub and respond with results.

In the `_heartbeat_loop`, after receiving the hub's response to the heartbeat, check for any pending command messages. Actually, the architecture needs adjustment: the hub sends command messages directly on the agent's WebSocket. The agent needs to handle both its own heartbeat sends and incoming command messages.

Modify `_heartbeat_loop` to use a reader/writer pattern:
1. After sending a heartbeat, read the response
2. If the response contains a `type: "run_command"` field, handle it
3. Use `asyncio.create_subprocess_shell` to run the command
4. Send the result back

Add to `agent.py`:

```python
async def _handle_command(self, data: dict) -> dict:
    """Handle a remote command request from the hub."""
    import asyncio as _asyncio
    cmd_id = data.get("cmd_id", "")
    command = data.get("command", "")
    logger.info("running_remote_command", cmd_id=cmd_id[:8], command=command[:80])
    try:
        proc = await _asyncio.create_subprocess_shell(
            command,
            stdout=_asyncio.subprocess.PIPE,
            stderr=_asyncio.subprocess.PIPE,
        )
        stdout, stderr = await _asyncio.wait_for(proc.communicate(), timeout=30.0)
        return {
            "type": "command_result",
            "cmd_id": cmd_id,
            "stdout": stdout.decode(errors="replace"),
            "stderr": stderr.decode(errors="replace"),
            "exit_code": proc.returncode,
        }
    except _asyncio.TimeoutError:
        return {
            "type": "command_result",
            "cmd_id": cmd_id,
            "stdout": "",
            "stderr": "Command timed out after 30s",
            "exit_code": -1,
        }
    except Exception as exc:
        return {
            "type": "command_result",
            "cmd_id": cmd_id,
            "stdout": "",
            "stderr": str(exc),
            "exit_code": -1,
        }
```

**Step 6: Run tests**

```bash
pytest tests/test_executor.py -v
```

Expected: 3 PASS

```bash
pytest -v
```

Expected: All tests pass

**Step 7: Commit**

```bash
git add soul_mesh/executor.py soul_mesh/server.py soul_mesh/agent.py tests/test_executor.py
git commit -m "feat(dashboard): add remote command relay with executor module"
```

---

### Task 4: Create Textual dashboard -- Cluster Overview + Node Detail screens

The main dashboard module with the Cluster Overview (default screen) showing a live-updating node table with sparklines, and a Node Detail screen showing full specs and graphs.

**Files:**
- Create: `soul_mesh/dashboard.py`
- Modify: `soul_mesh/cli.py` (add `dashboard` command)
- Create: `tests/test_dashboard.py`

**Step 1: Write the failing test**

```python
# tests/test_dashboard.py
import pytest


async def test_dashboard_module_imports():
    from soul_mesh.dashboard import MeshDashboard
    assert MeshDashboard is not None


async def test_dashboard_app_instantiation():
    from soul_mesh.dashboard import MeshDashboard
    app = MeshDashboard(hub_url="http://localhost:8340")
    assert app.hub_url == "http://localhost:8340"
```

**Step 2: Run test to verify it fails**

```bash
pytest tests/test_dashboard.py -v
```

Expected: FAIL with `ModuleNotFoundError`

**Step 3: Implement dashboard.py**

Create `soul_mesh/dashboard.py` with:

1. `MeshDashboard(App)` -- main Textual app
2. `ClusterOverview(Screen)` -- default screen with node table
3. `NodeDetail(Screen)` -- drill-down for a selected node
4. API polling via `httpx.AsyncClient` every 3s using `set_interval`
5. Sparkline widgets for CPU/RAM (last 30 data points)
6. Color coding: red for stale nodes, yellow for high RAM usage (>85%)

Key implementation details:
- Use `textual.widgets.DataTable` for the node list
- Use `textual.widgets.Sparkline` for CPU/RAM history
- Use `textual.widgets.Header`, `Footer` for chrome
- Store hub_url as instance variable
- Poll `/api/mesh/nodes` and `/api/mesh/status` for overview
- Poll `/api/mesh/nodes/{id}/heartbeats` for node detail
- Keybindings: `q` quit, `r` refresh, `enter` drill into node, `escape` back, `1-5` switch screens

The full implementation should be ~200 lines for these two screens.

**Step 4: Add CLI command**

Add to `soul_mesh/cli.py`:

```python
@main.command()
@click.option("--hub", "hub_url", default="http://localhost:8340", help="Hub URL.")
def dashboard(hub_url: str):
    """Launch the TUI dashboard."""
    from soul_mesh.dashboard import MeshDashboard
    app = MeshDashboard(hub_url=hub_url)
    app.run()
```

**Step 5: Run tests**

```bash
pytest tests/test_dashboard.py -v
```

Expected: 2 PASS

```bash
pytest -v
```

Expected: All tests pass

**Step 6: Commit**

```bash
git add soul_mesh/dashboard.py soul_mesh/cli.py tests/test_dashboard.py
git commit -m "feat(dashboard): add Cluster Overview and Node Detail screens"
```

---

### Task 5: Add Alerts screen

A scrolling log of cluster events: node went stale, node came online, RAM threshold crossed. Stored in memory (deque) during the dashboard session, populated by comparing successive API polls.

**Files:**
- Modify: `soul_mesh/dashboard.py` (add AlertsScreen, alert generation logic)
- Modify: `tests/test_dashboard.py` (add alert tests)

**Step 1: Write the failing test**

```python
async def test_alert_generation_node_went_stale():
    from soul_mesh.dashboard import generate_alerts
    old_nodes = [{"id": "n1", "name": "pi", "status": "online", "ram_used_percent": 50}]
    new_nodes = [{"id": "n1", "name": "pi", "status": "stale", "ram_used_percent": 50}]
    alerts = generate_alerts(old_nodes, new_nodes)
    assert len(alerts) == 1
    assert "stale" in alerts[0]["message"].lower()
    assert alerts[0]["severity"] == "warning"


async def test_alert_generation_high_ram():
    from soul_mesh.dashboard import generate_alerts
    old_nodes = [{"id": "n1", "name": "pi", "status": "online", "ram_used_percent": 50}]
    new_nodes = [{"id": "n1", "name": "pi", "status": "online", "ram_used_percent": 90}]
    alerts = generate_alerts(old_nodes, new_nodes)
    assert any("ram" in a["message"].lower() for a in alerts)


async def test_alert_generation_node_came_online():
    from soul_mesh.dashboard import generate_alerts
    old_nodes = [{"id": "n1", "name": "pi", "status": "stale", "ram_used_percent": 50}]
    new_nodes = [{"id": "n1", "name": "pi", "status": "online", "ram_used_percent": 50}]
    alerts = generate_alerts(old_nodes, new_nodes)
    assert any("online" in a["message"].lower() for a in alerts)
```

**Step 2: Run test to verify it fails**

```bash
pytest tests/test_dashboard.py::test_alert_generation_node_went_stale -v
```

Expected: FAIL with `ImportError: cannot import name 'generate_alerts'`

**Step 3: Implement alerts**

Add `generate_alerts(old_nodes, new_nodes) -> list[dict]` function to `dashboard.py`. Each alert has: `timestamp`, `severity` (info/warning/critical), `message`.

Add `AlertsScreen(Screen)` to the dashboard with a scrolling `RichLog` widget. The main app calls `generate_alerts` on each poll cycle and appends new alerts to the screen.

**Step 4: Run tests**

```bash
pytest tests/test_dashboard.py -v
```

Expected: All PASS

**Step 5: Commit**

```bash
git add soul_mesh/dashboard.py tests/test_dashboard.py
git commit -m "feat(dashboard): add Alerts screen with event detection"
```

---

### Task 6: Add Remote Shell screen

A screen where you select a node, type a command, and see stdout/stderr. Uses the `/api/mesh/run` endpoint from Task 3.

**Files:**
- Modify: `soul_mesh/dashboard.py` (add RemoteShellScreen)
- Modify: `tests/test_dashboard.py` (add remote shell tests)

**Step 1: Write the failing test**

```python
async def test_remote_shell_screen_exists():
    from soul_mesh.dashboard import RemoteShellScreen
    assert RemoteShellScreen is not None
```

**Step 2: Run test to verify it fails**

```bash
pytest tests/test_dashboard.py::test_remote_shell_screen_exists -v
```

Expected: FAIL with `ImportError`

**Step 3: Implement RemoteShellScreen**

Add `RemoteShellScreen(Screen)` to `dashboard.py`:
- `Select` widget to pick a node (populated from `/api/mesh/nodes`)
- `Input` widget for command entry
- `RichLog` widget for output display
- On submit: POST to `/api/mesh/run` with `node_id` and `command`
- Display stdout in green, stderr in red, exit code in header

**Step 4: Run tests**

```bash
pytest tests/test_dashboard.py -v && pytest -v
```

Expected: All PASS

**Step 5: Commit**

```bash
git add soul_mesh/dashboard.py tests/test_dashboard.py
git commit -m "feat(dashboard): add Remote Shell screen"
```

---

### Task 7: Final verification, update README, and push

**Files:**
- Modify: `README.md` (add dashboard section, update test count)

**Step 1: Run full test suite**

```bash
pytest -v
```

Expected: All tests pass (203 existing + new tests from this plan)

**Step 2: Verify CLI command works**

```bash
soul-mesh dashboard --help
```

Expected: Shows help text with `--hub` option

**Step 3: Verify zero import issues**

```bash
grep -r "from brain" soul_mesh/
```

Expected: No output (zero matches)

**Step 4: Update README**

Add a Dashboard section to `README.md` describing the new `soul-mesh dashboard` command. Update the test count table.

**Step 5: Commit and push**

```bash
git add README.md
git commit -m "docs: add dashboard section to README, update test counts"
git push origin master
git push github master
```

---

## Summary

| Task | What | New Files | Tests |
|------|------|-----------|-------|
| 1 | Add textual/httpx deps | - | - |
| 2 | Heartbeat history endpoint | - | 3 |
| 3 | Remote command relay (executor) | executor.py | 3 |
| 4 | Cluster Overview + Node Detail screens | dashboard.py | 2 |
| 5 | Alerts screen | - | 3 |
| 6 | Remote Shell screen | - | 1 |
| 7 | Final verification + README + push | - | - |

Total: 7 tasks, 2 new modules, ~12 new tests
